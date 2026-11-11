# uklive-status

Scheduled job for the UKLIVE app. Every 5 minutes a GitHub Actions run checks
the UKLIVE YouTube channel once, centrally, and writes the result to Firestore
so phones never need their own YouTube API calls.

## What it does
`check_live.py` (run by `.github/workflows/check-live.yml`):
1. `playlistItems.list` on the channel's uploads playlist (1 unit).
2. `videos.list` (snippet + liveStreamingDetails) for those videos, plus the
   video on the channel's public /live page and the last known live/next ids
   (1 unit). Finds the live video and the next scheduled stream.
3. Writes Firestore doc `status/live` in project `uklive-f7b27` (UKLIVE-V2):
   `isLive, liveVideoId, streamUrl, feedUrl, latestVideoId, nextTitle,
   nextVideoId, nextStart, updatedAt, source, lastError, runsToday, runsDate,
   unitsToday`.

If YouTube fails, only `updatedAt`, `lastError` and the counters change; the
last good live fields are kept. The run fails (red) only if Firestore fails.

## Quota
About 2 units per run x 288 runs/day = about 576 units/day of the free
10,000/day. Shown live in the `unitsToday` field.

## Secrets (Settings > Secrets and variables > Actions)
- `YOUTUBE_API_KEY`: YouTube Data API v3 key.
- `GCP_SA_KEY`: JSON key of `uklive-status-writer@uklive-f7b27.iam.gserviceaccount.com`
  (role: Cloud Datastore User).
Never commit either value.

## Run by hand / test
- GitHub: Actions > check-live > Run workflow.
- Local, no Firestore: `YOUTUBE_API_KEY=... python check_live.py --dry-run`

## Pause
Actions > check-live > "..." > Disable workflow (Enable to resume).
Note: GitHub auto-disables scheduled workflows in public repos after 60 days
with no repository activity; re-enable it there if that happens.
