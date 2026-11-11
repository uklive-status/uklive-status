#!/usr/bin/env python3
"""UKLIVE live-status checker. Runs on a schedule (GitHub Actions).

Reads the channel's uploads playlist (1 unit) and videos.list (1 unit), then
writes the result to Firestore doc status/live in project uklive-f7b27.
Env: YOUTUBE_API_KEY, GCP_SA_KEY (service account JSON). Never logs the key.
Usage: python check_live.py [--dry-run]   (--dry-run skips Firestore)
"""
import datetime as dt, json, os, re, sys
import requests

CHANNEL_ID = os.environ.get("YT_CHANNEL_ID", "UCm2h0e7F7aY8TpVtu9xx7WQ")
PROJECT = os.environ.get("FIRESTORE_PROJECT", "uklive-f7b27")
EMU = os.environ.get("FIRESTORE_EMULATOR_HOST", "")  # local tests only
DOC_URL = (f"{'http://' + EMU if EMU else 'https://firestore.googleapis.com'}/v1/projects/{PROJECT}"
           "/databases/(default)/documents/status/live")
MAX_RESULTS = int(os.environ.get("YT_MAX_RESULTS", "45"))  # 1 unit for 1..50
YT = "https://www.googleapis.com/youtube/v3/"
KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
LIVE_FIELDS = ["isLive", "liveVideoId", "streamUrl", "latestVideoId",
               "nextTitle", "nextVideoId", "nextStart"]

def scrub(s):  # never let the key leak into logs or Firestore
    return str(s).replace(KEY, "***") if KEY else str(s)

def yt(path, **params):
    r = requests.get(YT + path, params={**params, "key": KEY}, timeout=15)
    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:200]
        raise RuntimeError(f"{path} HTTP {r.status_code}: {msg}")
    return r.json().get("items", [])

def live_page_id():  # free (0 units): the channel /live page's current video id
    try:
        r = requests.get(f"https://www.youtube.com/channel/{CHANNEL_ID}/live", timeout=15,
                         headers={"User-Agent": "Mozilla/5.0", "Cookie": "CONSENT=YES+1"})
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', r.text)
        return m.group(1) if m else ""
    except Exception:
        return ""

def check_youtube(now, extra_ids=()):
    items = yt("playlistItems", part="contentDetails", maxResults=MAX_RESULTS,
               playlistId="UU" + CHANNEL_ID[2:])
    ids = [i["contentDetails"]["videoId"] for i in items]
    # Streams scheduled days ahead sit deep in uploads, so also check the /live
    # page id and the last known live/next ids (same videos.list call, max 50).
    for x in [live_page_id(), *extra_ids]:
        if x and x not in ids:
            ids.insert(0, x)
    ids = ids[:50]
    out = {"isLive": False, "liveVideoId": "", "streamUrl": "", "nextTitle": "",
           "nextVideoId": "", "nextStart": "", "latestVideoId": items[0]["contentDetails"]["videoId"] if items else ""}
    if not ids:
        return out
    upcoming = []
    for v in yt("videos", part="snippet,liveStreamingDetails", id=",".join(ids)):
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
    return out

def to_fs(v):
    if isinstance(v, bool): return {"booleanValue": v}
    if isinstance(v, int): return {"integerValue": str(v)}
    return {"stringValue": str(v)}

def main():
    dry = "--dry-run" in sys.argv
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
    same_day = old.get("runsDate", {}).get("stringValue") == today
    runs = int(old.get("runsToday", {}).get("integerValue", 0)) if same_day else 0
    units = int(old.get("unitsToday", {}).get("integerValue", 0)) if same_day else 0
    doc = {"feedUrl": f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}",
           "source": "github-actions", "runsToday": runs + 1, "runsDate": today,
           "unitsToday": units + 2, "lastError": ""}
    try:
        if not KEY:
            raise RuntimeError("YOUTUBE_API_KEY is not set")
        known = [old.get(k, {}).get("stringValue", "") for k in ("liveVideoId", "nextVideoId")]
        doc.update(check_youtube(now, known))
    except Exception as e:  # keep last good live fields; just record the error
        doc["lastError"] = scrub(e)[:500]
    if dry:
        print(json.dumps({**doc, "updatedAt": now, "_dryRun": True}, indent=2))
        return
    fields = {k: to_fs(v) for k, v in doc.items()}
    fields["updatedAt"] = {"timestampValue": now}
    mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
    r = session.patch(f"{DOC_URL}?{mask}", json={"fields": fields}, timeout=15)
    if r.status_code != 200:
        sys.exit(f"Firestore write failed: HTTP {r.status_code} {r.text[:300]}")
    print(f"ok {now} isLive={doc.get('isLive', 'kept')} live={doc.get('liveVideoId', 'kept')} "
          f"next={doc.get('nextVideoId', 'kept')} runsToday={doc['runsToday']} "
          f"error={doc['lastError'] or '-'}")

if __name__ == "__main__":
    main()
