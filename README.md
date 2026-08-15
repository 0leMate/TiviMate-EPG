# Automatic TiviMate event EPG

This repository downloads an Xtream M3U playlist and regenerates
`vortex_custom_event_epg.xml` every eight hours.

## Setup

1. Upload all files and folders from this package to the root of a GitHub repository.
2. Open **Settings → Secrets and variables → Actions**.
3. Create a repository secret named `XTREAM_M3U_URL`.
4. Set its value to your complete Xtream M3U URL.
5. Open **Actions → Refresh TiviMate EPG → Run workflow** for the first run.
6. Open `vortex_custom_event_epg.xml`, click **Raw**, and use that Raw URL in TiviMate.

The secret must look like:

`https://YOUR-SERVER/get.php?username=YOUR_USERNAME&password=YOUR_PASSWORD&type=m3u_plus&output=ts`

Do not paste credentials into `generate_epg.py`, the workflow file, or README.

## TiviMate

Add the Raw XML URL under:

**Settings → EPG → EPG sources → Add source**

Then associate it with the Xtream playlist and set TiviMate's EPG update interval to
8 hours where that option is available.

## Notes

- Paramount listings are parsed from the dated event names supplied in the M3U.
- Sky Sports+ currently has empty event names in the supplied playlist, so the generator
  publishes a placeholder until the provider adds real event details.
- GitHub scheduled jobs can start a little later than the exact cron time.
