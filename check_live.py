#!/usr/bin/env python3
"""UKLIVE live-status checker. Runs on a schedule (GitHub Actions).

Each run (about 3 YouTube units):
  1. playlistItems.list on the uploads playlist, 50 items (1 unit).
  2. videos.list on those ids plus the /live page id and the last known
     live/next ids (1 unit per 50 ids, normally 2).
  3. Writes Firestore doc status/live in project uklive-f7b27 (unchanged).
  4. Writes live.json (--out): the same live fields plus `shows`, the last 50
     past videos, for the app to read with no key from the repo's `data` branch.
Every ~6 h the kept shows are re-checked (1 unit) so deleted/private videos
drop out; page 2 of uploads is read (2 units) only if fewer than 50 shows.
Env: YOUTUBE_API_KEY, GCP_SA_KEY (service account JSON). Never logs the key.
Usage: python check_live.py [--dry-run] [--out live.json] [--old old_live.json]
       (--dry-run skips Firestore)
"""
import datetime as dt, json, os, re, sys
import requests

CHANNEL_ID = os.environ.get("YT_CHANNEL_ID", "UCm2h0e7F7aY8TpVtu9xx7WQ")
PROJECT = os.environ.get("FIRESTORE_PROJECT", "uklive-f7b27")
EMU = os.environ.get("FIRESTORE_EMULATOR_HOST", "")  # local tests only
DOC_URL = (f"{'http://' + EMU if EMU else 'https://firestore.googleapis.com'}/v1/projects/{PROJECT}"
           "/databases/(default)/documents/status/live")
MAX_RESULTS = int(os.environ.get("YT_MAX_RESULTS", "50"))  # 1 unit for 1..50
SHOWS_MAX = 50
RECHECK_HOURS = 6
RAW_LIVE_JSON = "https://raw.githubusercontent.com/uklive-status/uklive-status/data/live.json"
YT = "https://www.googleapis.com/youtube/v3/"
KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
LIVE_FIELDS = ["isLive", "liveVideoId", "streamUrl", "latestVideoId",
               "nextTitle", "nextVideoId", "nextStart"]
UNITS = [0]  # YouTube quota units used by this run

def scrub(s):  # never let the key leak into logs, Firestore or live.json
    s = str(s)
    if KEY:
        s = s.replace(KEY, "***")
    return re.sub(r"AIza[0-9A-Za-z_-]{20,}", "***", s)

def yt(path, **params):
    UNITS[0] += 1
    r = requests.get(YT + path, params={**params, "key": KEY}, timeout=15)
    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:200]
        raise RuntimeError(f"{path} HTTP {r.status_code}: {msg}")
    return r.json()

def live_page_id():  # free (0 units): the channel /live page's current video id
    try:
        r = requests.get(f"https://www.youtube.com/channel/{CHANNEL_ID}/live", timeout=15,
                         headers={"User-Agent": "Mozilla/5.0", "Cookie": "CONSENT=YES+1"})
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', r.text)
        return m.group(1) if m else ""
    except Exception:
        return ""

def playlist_page(token=""):
    p = dict(part="contentDetails", maxResults=MAX_RESULTS, playlistId="UU" + CHANNEL_ID[2:])
    if token:
        p["pageToken"] = token
    j = yt("playlistItems", **p)
    ids = [i["contentDetails"]["videoId"] for i in j.get("items", []) if i.get("contentDetails", {}).get("videoId")]
    return ids, j.get("nextPageToken", "")

def videos(ids):
    """videos.list in chunks of 50 (1 unit each). Missing ids = private/deleted."""
    out, ids = {}, list(dict.fromkeys(i for i in ids if i))
    for n in range(0, len(ids), 50):
        for v in yt("videos", part="snippet,liveStreamingDetails,status",
                    id=",".join(ids[n:n + 50])).get("items", []):
            out[v["id"]] = v
    return out

def show_item(v):
    """A past show for the app, or None (upcoming, live, private, never streamed)."""
    sn, lsd = v.get("snippet") or {}, v.get("liveStreamingDetails") or {}
    if sn.get("liveBroadcastContent", "none") != "none":
        return None  # upcoming placeholder or live now
    if (v.get("status") or {}).get("privacyStatus", "public") != "public":
        return None
    if lsd and not lsd.get("actualStartTime"):
        return None  # scheduled stream that never started
    th = sn.get("thumbnails") or {}
    t = th.get("high") or th.get("medium") or th.get("default") or {}
    return {"videoId": v["id"], "title": sn.get("title", ""),
            "publishedAt": lsd.get("actualStartTime") or sn.get("publishedAt", ""),
            "thumbnailUrl": t.get("url") or f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg"}

def merge_shows(old, fetched, vids):
    """old shows + fresh ones; any id we just fetched uses the fresh verdict."""
    by = {s["videoId"]: s for s in old if isinstance(s, dict) and s.get("videoId")}
    for i in fetched:
        s = show_item(vids[i]) if i in vids else None
        if s:
            by[i] = s
        else:
            by.pop(i, None)  # now private/deleted/upcoming/live
    return sorted(by.values(), key=lambda s: s.get("publishedAt", ""), reverse=True)[:SHOWS_MAX]

def check_youtube(now, extra_ids=(), old_shows=(), shows_checked=""):
    ids, token = playlist_page()
    latest = ids[0] if ids else ""
    # Streams scheduled days ahead sit deep in uploads, so also check the /live
    # page id and the last known live/next ids.
    extra = [x for x in dict.fromkeys([live_page_id(), *extra_ids]) if x and x not in ids]
    all_ids = extra + ids
    vids = videos(all_ids)
    out = {"isLive": False, "liveVideoId": "", "streamUrl": "", "nextTitle": "",
           "nextVideoId": "", "nextStart": "", "latestVideoId": latest}
    upcoming = []
    for i in all_ids:
        v = vids.get(i)
        if not v:
            continue
        state = v["snippet"].get("liveBroadcastContent")
        if state == "live" and not out["isLive"]:
            out.update(isLive=True, liveVideoId=v["id"],
                       streamUrl=f"https://www.youtube.com/watch?v={v['id']}")
        start = (v.get("liveStreamingDetails") or {}).get("scheduledStartTime", "")
        if state == "upcoming" and start and start > now:
            upcoming.append((start, v))
    if upcoming:
        start, v = min(upcoming, key=lambda t: t[0])
        out.update(nextTitle=v["snippet"].get("title", ""), nextVideoId=v["id"], nextStart=start)

    # Past shows: fresh page 1 + extras, merged with the last good list.
    shows = merge_shows(old_shows, all_ids, vids)
    checked = shows_checked
    try:
        age = dt.datetime.fromisoformat(now.replace("Z", "+00:00")) - \
              dt.datetime.fromisoformat(shows_checked.replace("Z", "+00:00"))
        stale = age > dt.timedelta(hours=RECHECK_HOURS)
    except Exception:
        stale = True
    if stale:  # re-check kept shows we didn't just fetch (deleted/private drop out)
        kept = [s["videoId"] for s in shows if s["videoId"] not in vids]
        if kept:
            v2 = videos(kept)
            vids.update(v2)
            shows = merge_shows(shows, kept, vids)
        checked = now
    if len(shows) < SHOWS_MAX and token:  # top up from page 2 (first run / gaps)
        ids2, _ = playlist_page(token)
        ids2 = [i for i in ids2 if i not in vids]
        vids.update(videos(ids2))
        shows = merge_shows(shows, ids2, vids)
    out["shows"] = shows
    out["showsCheckedAt"] = checked
    return out

def to_fs(v):
    if isinstance(v, bool): return {"booleanValue": v}
    if isinstance(v, int): return {"integerValue": str(v)}
    return {"stringValue": str(v)}

def load_old_json(path):
    try:
        if path and os.path.exists(path):
            with open(path) as f:
                j = json.load(f)
        else:
            r = requests.get(RAW_LIVE_JSON, timeout=10)
            j = r.json() if r.status_code == 200 else {}
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}

def arg(name):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else ""

def main():
    dry = "--dry-run" in sys.argv
    out_path, old_path = arg("--out"), arg("--old")
    now_dt = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now_dt.strftime("%Y-%m-%d")
    if "bc919" in PROJECT:
        sys.exit("Refusing to write to uklive-bc919.")
    session, old = None, {}
    if not dry and EMU:
        session = requests.Session()
        session.headers["Authorization"] = "Bearer owner"
    elif not dry:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        creds = service_account.Credentials.from_service_account_info(
            json.loads(os.environ["GCP_SA_KEY"]),
            scopes=["https://www.googleapis.com/auth/datastore"])
        session = AuthorizedSession(creds)
    if session:
        r = session.get(DOC_URL, timeout=15)
        if r.status_code == 200:
            old = r.json().get("fields", {})
        elif r.status_code != 404:
            sys.exit(f"Firestore read failed: HTTP {r.status_code} {r.text[:300]}")
    old_json = load_old_json(old_path) if out_path else {}
    same_day = old.get("runsDate", {}).get("stringValue") == today
    runs = int(old.get("runsToday", {}).get("integerValue", 0)) if same_day else 0
    units = int(old.get("unitsToday", {}).get("integerValue", 0)) if same_day else 0
    doc = {"feedUrl": f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}",
           "source": "github-actions", "runsToday": runs + 1, "runsDate": today,
           "unitsToday": units, "lastError": ""}
    res = {}
    try:
        if not KEY:
            raise RuntimeError("YOUTUBE_API_KEY is not set")
        known = [old.get(k, {}).get("stringValue", "") or str(old_json.get(k, ""))
                 for k in ("liveVideoId", "nextVideoId")]
        old_shows = old_json.get("shows") if isinstance(old_json.get("shows"), list) else []
        res = check_youtube(now, known, old_shows, str(old_json.get("showsCheckedAt", "")))
        doc.update({k: res[k] for k in LIVE_FIELDS})
    except Exception as e:  # keep last good live fields; just record the error
        doc["lastError"] = scrub(e)[:500]
    doc["unitsToday"] = units + UNITS[0]

    if out_path:
        pub = {k: old_json.get(k, "" if k != "isLive" else False) for k in LIVE_FIELDS}
        if res:
            pub.update({k: res[k] for k in LIVE_FIELDS})
        shows = res.get("shows") if res else old_json.get("shows")
        pub.update({"updatedAt": now, "lastError": doc["lastError"],
                    "showsCheckedAt": res.get("showsCheckedAt") if res else old_json.get("showsCheckedAt", ""),
                    "showsCount": len(shows or []), "shows": shows or [],
                    "source": "github-actions", "unitsThisRun": UNITS[0]})
        text = scrub(json.dumps(pub, ensure_ascii=False, indent=1))
        with open(out_path, "w") as f:
            f.write(text + "\n")
        print(f"live.json written: shows={pub['showsCount']} units={UNITS[0]} isLive={pub['isLive']}")
    if dry:
        print(scrub(json.dumps({**doc, "updatedAt": now, "_dryRun": True}, indent=2)))
        return
    fields = {k: to_fs(v) for k, v in doc.items()}
    fields["updatedAt"] = {"timestampValue": now}
    mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
    r = session.patch(f"{DOC_URL}?{mask}", json={"fields": fields}, timeout=15)
    if r.status_code != 200:
        sys.exit(f"Firestore write failed: HTTP {r.status_code} {r.text[:300]}")
    print(f"ok {now} isLive={doc.get('isLive', 'kept')} live={doc.get('liveVideoId', 'kept')} "
          f"next={doc.get('nextVideoId', 'kept')} runsToday={doc['runsToday']} "
          f"units={UNITS[0]} error={doc['lastError'] or '-'}")

if __name__ == "__main__":
    main()
