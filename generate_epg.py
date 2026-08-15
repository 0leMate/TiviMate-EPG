#!/usr/bin/env python3
from __future__ import annotations

import html
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

OUTPUT = Path("vortex_custom_event_epg.xml")

GROUP_TIMEZONES = {
    "PAY PER VIEW": "America/New_York",
    "PAY PER VIEW 2": "America/New_York",
    "US PARAMOUNT": "America/New_York",
    "AU STAN": "Australia/Sydney",
    "AU KAYO+": "Australia/Sydney",
    "US ESPN+": "America/New_York",
    "DIRTVISION": "America/New_York",
    "UFC": "America/New_York",
    "CA SPORTSNET+": "America/Toronto",
    "CA TSN+": "America/Toronto",
    "SKY SPORTS+": "Europe/London",
}

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
DATE_RE = re.compile(r'(?P<date>\b\d{2}-\d{2}\b)\s*$')
TIME_RE = re.compile(
    r'^\s*(?P<time>\d{1,2}:\d{2})(?:\s*(?P<ampm>AM|PM))?\s+(?P<title>.+?)\s*$',
    re.IGNORECASE,
)


def attrs(line: str) -> dict[str, str]:
    return dict(ATTR_RE.findall(line))


def stable_name(display: str) -> str:
    return display.split(":", 1)[0].strip() if ":" in display else display.strip()


def safe_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", ".", value.strip())
    return value.strip(".") or "channel"


def xmltv_dt(value: datetime) -> str:
    return value.strftime("%Y%m%d%H%M%S %z")


def infer_year(month: int, day: int, timezone: ZoneInfo) -> int:
    """Choose the closest sensible year for an MM-DD provider listing."""
    now = datetime.now(timezone)
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            dt = datetime(year, month, day, tzinfo=timezone)
        except ValueError:
            continue
        candidates.append(dt)
    return min(candidates, key=lambda d: abs((d - now).total_seconds())).year


def parse_event(display: str, timezone_name: str):
    if ":" not in display:
        return None

    payload = display.split(":", 1)[1].strip()
    date_match = DATE_RE.search(payload)
    if not date_match:
        return None

    month, day = map(int, date_match.group("date").split("-"))
    without_date = payload[: date_match.start()].strip()
    time_match = TIME_RE.match(without_date)
    if not time_match:
        return None

    hour, minute = map(int, time_match.group("time").split(":"))
    ampm = time_match.group("ampm")
    if ampm:
        if ampm.upper() == "PM" and hour != 12:
            hour += 12
        elif ampm.upper() == "AM" and hour == 12:
            hour = 0

    timezone = ZoneInfo(timezone_name)
    year = infer_year(month, day, timezone)
    start = datetime(year, month, day, hour, minute, tzinfo=timezone)
    title = time_match.group("title").strip()
    return start, start + timedelta(hours=4), title


def download_playlist() -> str:
    url = os.environ.get("XTREAM_M3U_URL", "").strip()
    if not url:
        raise RuntimeError("XTREAM_M3U_URL repository secret is missing.")

    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 TiviMate-EPG-Updater/1.0"},
    )
    with urlopen(request, timeout=90) as response:
        data = response.read()

    text = data.decode("utf-8", errors="replace")
    if "#EXTM3U" not in text:
        raise RuntimeError("The downloaded response was not an M3U playlist.")
    return text


def main() -> int:
    playlist = download_playlist()
    lines = playlist.splitlines()

    channels = []
    programmes = []
    now_utc = datetime.now(ZoneInfo("UTC"))

    for line in lines:
        if not line.startswith("#EXTINF:"):
            continue

        metadata = attrs(line)
        group = metadata.get("group-title", "").strip()
        if group not in GROUP_TIMEZONES:
            continue

        display = line.split(",", 1)[1].strip() if "," in line else metadata.get("tvg-name", "")
        base = stable_name(display)

        # Group + stable slot creates a predictable, collision-free ID.
        channel_id = f"custom.{safe_id(group)}.{safe_id(base)}"
        channels.append((channel_id, base, display, group))

        parsed = parse_event(display, GROUP_TIMEZONES[group])
        if parsed:
            start, stop, title = parsed
            programmes.append((channel_id, start, stop, title, group))
        elif group == "SKY SPORTS+":
            # The provider currently supplies blank Sky Sports+ slot names.
            # Add a rolling placeholder so TiviMate confirms the EPG is assigned.
            start = now_utc.replace(minute=0, second=0, microsecond=0)
            stop = start + timedelta(days=7)
            programmes.append(
                (channel_id, start, stop, "No event currently listed", group)
            )

    # De-duplicate channels while retaining playlist order.
    unique_channels = []
    seen = set()
    for item in channels:
        if item[0] not in seen:
            seen.add(item[0])
            unique_channels.append(item)

    output = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="Automatic Vortex Event EPG">',
    ]

    for channel_id, base, display, group in unique_channels:
        output.append(f'  <channel id="{html.escape(channel_id, quote=True)}">')
        output.append(f'    <display-name>{html.escape(base)}</display-name>')
        if display and display != base:
            output.append(f'    <display-name>{html.escape(display)}</display-name>')
        output.append(f'    <display-name>{html.escape(group)}</display-name>')
        output.append("  </channel>")

    for channel_id, start, stop, title, group in programmes:
        output.append(
            f'  <programme start="{xmltv_dt(start)}" stop="{xmltv_dt(stop)}" '
            f'channel="{html.escape(channel_id, quote=True)}">'
        )
        output.append(f'    <title>{html.escape(title)}</title>')
        output.append("    <category>Sports</category>")
        output.append(f'    <desc>{html.escape(group)} event</desc>')
        output.append("  </programme>")

    output.append("</tv>")
    OUTPUT.write_text("\n".join(output) + "\n", encoding="utf-8")
    print(
        f"Generated {OUTPUT}: {len(unique_channels)} channels, "
        f"{len(programmes)} programme entries."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"EPG generation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
