"""Offline tests for check_live.py (mocked YouTube; no key, no Firestore).
Run: python -m unittest discover -s tests -v"""
import json, os, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["YOUTUBE_API_KEY"] = "TESTKEY-not-a-real-key-0000"
import check_live as cl

NOW = "2026-11-26T14:00:00Z"

def vid(i, state="none", start=None, priv="public", lsd=True, sched=None):
    v = {"id": i, "snippet": {"title": f"Show {i}", "liveBroadcastContent": state,
         "publishedAt": f"2026-10-01T00:00:00Z",
         "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{i}/hqdefault.jpg"}}},
         "status": {"privacyStatus": priv}}
    if lsd:
        d = {}
        if start: d["actualStartTime"] = start
        if sched: d["scheduledStartTime"] = sched
        v["liveStreamingDetails"] = d
    return v

class Resp:
    def __init__(self, j=None, code=200, text=""):
        self._j, self.status_code, self.text = j, code, text or json.dumps(j)
    def json(self): return self._j

def make_world(n_uploads=120, live_id=None, fail=False):
    # uploads newest first: u000 newest. u000-u003 are upcoming placeholders,
    # u004 is private (missing from videos.list), u005 deleted (missing).
    ups = [f"u{n:03d}xxxxxxx"[:11] for n in range(n_uploads)]
    world = {}
    for k, i in enumerate(ups):
        if k < 4:
            world[i] = vid(i, "upcoming", sched=f"2026-12-0{k+1}T19:00:00Z")
        elif k in (4, 5):
            continue
        else:
            day = 25 - (k // 4)
            world[i] = vid(i, start=f"2026-11-{max(day,1):02d}T{(k%24):02d}:00:00Z")
    if live_id:
        world[live_id] = vid(live_id, "live", start="2026-11-26T13:30:00Z")
    calls = []
    def get(url, params=None, timeout=None, headers=None):
        calls.append((url, dict(params or {})))
        if fail and "googleapis" in url:
            return Resp({"error": {"message": "quotaExceeded key=" + cl.KEY}}, 403)
        if url.endswith("/live"):
            return Resp(None, 200, text=(f'<link rel="canonical" href="https://www.youtube.com/watch?v={live_id}">' if live_id else ""))
        if url.endswith("playlistItems"):
            start = int(params.get("pageToken", "0") or 0)
            page = ups[start:start + 50]
            j = {"items": [{"contentDetails": {"videoId": i}} for i in page]}
            if start + 50 < len(ups): j["nextPageToken"] = str(start + 50)
            return Resp(j)
        if url.endswith("videos"):
            ids = params["id"].split(",")
            assert len(ids) <= 50
            return Resp({"items": [world[i] for i in ids if i in world]})
        if url == cl.RAW_LIVE_JSON:
            return Resp({}, 404)
        raise AssertionError(url)
    return get, calls, ups

class T(unittest.TestCase):
    def setUp(self): cl.UNITS[0] = 0

    def test_first_run_fills_50_and_excludes(self):
        get, calls, ups = make_world()
        with mock.patch.object(cl.requests, "get", get):
            out = cl.check_youtube(NOW)
        ids = [s["videoId"] for s in out["shows"]]
        self.assertEqual(len(ids), 50)
        for bad in ups[:6]: self.assertNotIn(bad, ids)  # upcoming, private, deleted
        pubs = [s["publishedAt"] for s in out["shows"]]
        self.assertEqual(pubs, sorted(pubs, reverse=True))
        self.assertEqual(out["nextVideoId"], ups[0])
        self.assertEqual(out["nextStart"], "2026-12-01T19:00:00Z")
        self.assertFalse(out["isLive"])
        self.assertEqual(cl.UNITS[0], 4)  # page1 + videos + page2 + videos
        self.assertEqual(set(out["shows"][0]), {"videoId", "title", "publishedAt", "thumbnailUrl"})

    def test_steady_run_3_units_and_live(self):
        get, calls, ups = make_world(live_id="LIVEvid0001")
        with mock.patch.object(cl.requests, "get", get):
            first = cl.check_youtube(NOW)
            cl.UNITS[0] = 0
            out = cl.check_youtube(NOW, ("", ""), first["shows"], first["showsCheckedAt"])
        self.assertTrue(out["isLive"]); self.assertEqual(out["liveVideoId"], "LIVEvid0001")
        self.assertNotIn("LIVEvid0001", [s["videoId"] for s in out["shows"]])
        self.assertEqual(len(out["shows"]), 50)
        self.assertEqual(cl.UNITS[0], 3)  # page1 + 2 videos calls (51 ids)

    def test_ended_stream_deep_in_uploads_is_added(self):
        get, calls, ups = make_world()
        deep = "DEEPstream1"
        with mock.patch.object(cl.requests, "get", get):
            first = cl.check_youtube(NOW)
        # simulate: deep video (not in page 1) was live last run and has ended
        get2, _, _ = make_world()
        def g(url, params=None, **k):
            if url.endswith("videos") and deep in params["id"]:
                r = get2(url, params=params, **k)
                r._j["items"].append(vid(deep, start="2026-11-26T12:00:00Z"))
                return r
            return get2(url, params=params, **k)
        with mock.patch.object(cl.requests, "get", g):
            out = cl.check_youtube(NOW, (deep, ""), first["shows"], first["showsCheckedAt"])
        self.assertEqual(out["shows"][0]["videoId"], deep)
        self.assertEqual(len(out["shows"]), 50)

    def test_main_api_failure_keeps_last_good_and_scrubs(self):
        good = {"isLive": False, "liveVideoId": "", "nextTitle": "Next", "nextVideoId": "n1",
                "nextStart": "2026-12-01T19:00:00Z", "streamUrl": "", "latestVideoId": "u1",
                "shows": [{"videoId": "a", "title": "A", "publishedAt": "2026-11-25T00:00:00Z", "thumbnailUrl": "t"}],
                "showsCheckedAt": NOW}
        get, _, _ = make_world(fail=True)
        with tempfile.TemporaryDirectory() as d:
            old, out = os.path.join(d, "old.json"), os.path.join(d, "live.json")
            json.dump(good, open(old, "w"))
            with mock.patch.object(cl.requests, "get", get), \
                 mock.patch.object(sys, "argv", ["x", "--dry-run", "--out", out, "--old", old]):
                cl.main()
            text = open(out).read()
        j = json.loads(text)
        self.assertNotIn(cl.KEY, text)
        self.assertIn("quotaExceeded", j["lastError"])
        self.assertEqual(j["shows"], good["shows"]); self.assertEqual(j["nextTitle"], "Next")
        self.assertEqual(j["showsCount"], 1)

    def test_main_ok_writes_valid_json(self):
        get, _, _ = make_world()
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "live.json")
            with mock.patch.object(cl.requests, "get", get), \
                 mock.patch.object(sys, "argv", ["x", "--dry-run", "--out", out, "--old", os.path.join(d, "none.json")]):
                cl.main()
            j = json.load(open(out))
        for k in ["updatedAt", "isLive", "liveVideoId", "streamUrl", "latestVideoId", "nextTitle",
                  "nextVideoId", "nextStart", "lastError", "shows"]:
            self.assertIn(k, j)
        self.assertEqual(j["lastError"], ""); self.assertEqual(len(j["shows"]), 50)

    def test_recheck_drops_deleted(self):
        get, _, ups = make_world()
        with mock.patch.object(cl.requests, "get", get):
            first = cl.check_youtube(NOW)
            gone = first["shows"][-1]["videoId"]  # an old one (from page 2)
            # make it deleted, and make the recheck due
            get2, _, _ = make_world()
            def g(url, params=None, **k):
                r = get2(url, params=params, **k)
                if url.endswith("videos"):
                    r._j["items"] = [v for v in r._j["items"] if v["id"] != gone]
                return r
            with mock.patch.object(cl.requests, "get", g):
                out = cl.check_youtube("2026-11-26T21:00:01Z", (), first["shows"], first["showsCheckedAt"])
        self.assertNotIn(gone, [s["videoId"] for s in out["shows"]])
        self.assertEqual(out["showsCheckedAt"], "2026-11-26T21:00:01Z")

if __name__ == "__main__":
    unittest.main()
