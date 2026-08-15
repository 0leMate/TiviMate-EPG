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
DATE_MMDD_RE = re.compile(r'(?P<date>\b\d{2}-\d{2}\b)\s*$')
DATE_DDMM_RE = re.compile(r'(?P<date>\b\d{1,2}/\d{1,2}\b)\s*$')

# Time at beginning: 17:30 Event Name
TIME_FRONT_RE = re.compile(
    r'^\s*(?P<time>\d{1,2}:\d{2})(?:\s*(?P<ampm>AM|PM))?\s+(?P<title>.+?)\s*$',
    re.IGNORECASE,
)

# Time at end: Event Name 17:30
TIME_END_RE = re.compile(
    r'^\s*(?P<title>.+?)\s+(?P<time>\d{1,2}:\d{2})(?:\s*(?P<ampm>AM|PM))?\s*$',
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


def convert_hour(hour: int, ampm: str | None) -> int:
    if not ampm:
        return hour
    ampm = ampm.upper()
    if ampm == "PM" and hour != 12:
        return hour + 12
    if ampm == "AM" and hour == 12:
        return 0
    return hour


def closest_year(month: int, day: int, timezone: ZoneInfo) -> int:
    now = datetime.now(timezone)
    choices = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            choices.append(datetime(year, month, day, tzinfo=timezone))
        except ValueError:
            pass
    return min(choices, key=lambda d: abs((d - now).total_seconds())).year


def parse_event(display: str, timezone_name: str):
    """
    Handles all formats Vortex is currently using, including:
      PPV 04: 09:30 ACA 206 Vakhaev vs Aliakbari
      PPV2 01: 17:30 EARLY PRELIMS UFC 330 08-15
      PPV ALT 003: Shields vs Scott 01:30 16/08
      PPV ALT 005: UFC 330 ... 23:00
    When no date is supplied, the listing is treated as today's event in
    that group's local timezone. This works well because the playlist is
    refreshed every 8 hours.
    """
    if ":" not in display:
        return None

    payload = display.split(":", 1)[1].strip()
    if not payload:
        return None

    timezone = ZoneInfo(timezone_name)
    now = datetime.now(timezone)

    # Optional explicit date at the end.
    explicit_date = None
    m = DATE_MMDD_RE.search(payload)
    if m:
        month, day = map(int, m.group("date").split("-"))
        year = closest_year(month, day, timezone)
        explicit_date = (year, month, day)
        payload = payload[:m.start()].strip()
    else:
        m = DATE_DDMM_RE.search(payload)
        if m:
            day, month = map(int, m.group("date").split("/"))
            year = closest_year(month, day, timezone)
            explicit_date = (year, month, day)
            payload = payload[:m.start()].strip()

    # Try time at the start first, then at the end.
    m = TIME_FRONT_RE.match(payload)
    if not m:
        m = TIME_END_RE.match(payload)
    if not m:
        # There is still useful event text even if the provider omitted a time.
        # Show it as a rolling "current listing" programme.
        start = now - timedelta(hours=1)
        stop = now + timedelta(hours=12)
        return start, stop, payload

    title = m.group("title").strip()
    hour, minute = map(int, m.group("time").split(":"))
    hour = convert_hour(hour, m.group("ampm"))

    if explicit_date:
        year, month, day = explicit_date
    else:
        year, month, day = now.year, now.month, now.day

    start = datetime(year, month, day, hour, minute, tzinfo=timezone)

    # If no date was supplied and the time is very far from "today",
    # choose yesterday/tomorrow when that is clearly the nearer occurrence.
    if not explicit_date:
        candidates = [
            start - timedelta(days=1),
            start,
            start + timedelta(days=1),
        ]
        start = min(candidates, key=lambda d: abs((d - now).total_seconds()))

    # Four hours is a practical default for PPV/sports guide blocks.
    stop = start + timedelta(hours=4)
    return start, stop, title


def download_playlist() -> str:
    url = os.environ.get("XTREAM_M3U_URL", "").strip()
    if not url:
        raise RuntimeError("XTREAM_M3U_URL repository secret is missing.")

    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 TiviMate-EPG-Updater/1.1"},
    )
    with urlopen(request, timeout=90) as response:
        data = response.read()

    text = data.decode("utf-8", errors="replace")
    if "#EXTM3U" not in text:
        raise RuntimeError("The downloaded response was not an M3U playlist.")
    return text


def main() -> int:
    playlist = download_playlist()
    channels = []
    programmes = []
    now_utc = datetime.now(ZoneInfo("UTC"))

    for line in playlist.splitlines():
        if not line.startswith("#EXTINF:"):
            continue

        metadata = attrs(line)
        group = metadata.get("group-title", "").strip()
        if group not in GROUP_TIMEZONES:
            continue

        display = (
            line.split(",", 1)[1].strip()
            if "," in line
            else metadata.get("tvg-name", "").strip()
        )
        base = stable_name(display)
        channel_id = f"custom.{safe_id(group)}.{safe_id(base)}"
        channels.append((channel_id, base, display, group))

        parsed = parse_event(display, GROUP_TIMEZONES[group])
        if parsed:
            start, stop, title = parsed
            programmes.append((channel_id, start, stop, title, group))
        elif group == "SKY SPORTS+":
            start = now_utc.replace(minute=0, second=0, microsecond=0)
            stop = start + timedelta(days=7)
            programmes.append(
                (channel_id, start, stop, "No event currently listed", group)
            )

    # De-duplicate stable channel IDs while retaining provider order.
    unique_channels = []
    seen = set()
    for item in channels:
        if item[0] not in seen:
            seen.add(item[0])
            unique_channels.append(item)

    output = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="Automatic Vortex Event EPG v1.1">',
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
