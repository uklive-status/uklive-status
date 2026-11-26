# uklive-status

Scheduled job for the UKLIVE app. Every 5 minutes a GitHub Actions run checks
the UKLIVE YouTube channel once, centrally, and writes the result to Firestore
so phones never need their own YouTube API calls.

## What it does
`check_live.py` (run by `.github/workflows/check-live.yml`):
1. `playlistItems.list` on the channel's uploads playlist, 50 items (1 unit).
2. `videos.list` (snippet + liveStreamingDetails + status) for those videos, plus
   the video on the channel's public /live page and the last known live/next ids
   (1 unit per 50 ids, normally 2). Finds the live video and the next scheduled
   stream, and the past shows.
3. Writes Firestore doc `status/live` in project `uklive-f7b27` (UKLIVE-V2):
   `isLive, liveVideoId, streamUrl, feedUrl, latestVideoId, nextTitle,
   nextVideoId, nextStart, updatedAt, source, lastError, runsToday, runsDate,
   unitsToday`.

4. Writes `live.json` (job `check`) and force-pushes it as a single commit on
   branch `data` (job `publish`, the only job with `contents: write`, and it has
   no secrets). The app reads it with no key:
   https://raw.githubusercontent.com/uklive-status/uklive-status/data/live.json
   Fields: the live fields above plus `updatedAt`, `lastError`, `showsCount`,
   `showsCheckedAt`, `unitsThisRun` and `shows` = the last 50 past videos, newest
   first, each `{videoId, title, publishedAt, thumbnailUrl}` (publishedAt = stream
   start time for streams). Upcoming/scheduled placeholders, the live stream,
   private and deleted videos are left out. The list is merged with the last
   published one (so ended streams that sit deep in uploads stay in), re-checked
   every 6 h (1 unit) so deleted/private videos drop out, and topped up from
   uploads page 2 (2 units) only if it has fewer than 50.

If YouTube fails, only `updatedAt`, `lastError` and the counters change; the
last good live fields are kept. The run fails (red) only if Firestore fails.

## Quota
About 3 units per run (plus 1 unit every 6 h for the shows re-check). Runs come
about every 15 min (cron-job.org), so roughly 300-400 units/day of the free
10,000/day. Shown live in the `unitsToday` field.

## Secrets (Settings > Secrets and variables > Actions)
- `YOUTUBE_API_KEY`: YouTube Data API v3 key.
- `GCP_SA_KEY`: JSON key of `uklive-status-writer@uklive-f7b27.iam.gserviceaccount.com`
  (role: Cloud Datastore User).
Never commit either value.

## Run by hand / test
- GitHub: Actions > check-live > Run workflow.
- Local, no Firestore: `YOUTUBE_API_KEY=... python check_live.py --dry-run --out live.json`
- Offline tests (mocked YouTube, no key): `python -m unittest discover -s tests -v`

## Pause
Actions > check-live > "..." > Disable workflow (Enable to resume).
Note: GitHub auto-disables scheduled workflows in public repos after 60 days
with no repository activity; re-enable it there if that happens.
