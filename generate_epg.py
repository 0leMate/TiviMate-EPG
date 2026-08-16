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
    "UK DISCOVERY+": "Europe/London",
    "ESPN PLAY": "America/New_York",
}

WANTED_GROUPS = set(GROUP_TIMEZONES)

WANTED_PREFIXES = (
    "PPV ",
    "PPV2 ",
    "PPV ALT ",
    "PARAMOUNT ",
    "AU STAN ",
    "AU STAN ALT ",
    "KAYO+ ",
    "AU KAYO+ ",
    "ESPN+ ",
    "ESPN+ ALT ",
    "ESPN+ ALT2 ",
    "ESPNPLAY ",
    "DIRT ",
    "SKY SPORTS+ ",
    "SPORTSNET+ ",
    "SN+ ",
    "TSN+ ",
    "UFC ",
    "UK D+ ",
)

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')

# Beginning time, e.g. 17:30 Event Name
TIME_RE = re.compile(
    r'^\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})'
    r'(?:\s*(?P<ampm>AM|PM))?'
    r'\s*(?:\|\s*)?(?P<rest>.*)$',
    re.I,
)

# Date at the end:
# 08-28
# 08-28-26
# 8/28/26
# 16/08
DATE_RE = re.compile(
    r'(?:\s+|^)'
    r'(?P<a>\d{1,2})[-/](?P<b>\d{1,2})'
    r'(?:[-/](?P<year>\d{2,4}))?'
    r'\s*$'
)


def attrs(line: str) -> dict[str, str]:
    return dict(ATTR_RE.findall(line))


def safe_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", ".", value.strip())
    return value.strip(".") or "channel"


def stable_slot(display: str) -> str:
    return display.split(":", 1)[0].strip() if ":" in display else display.strip()


def wanted_channel(group: str, display: str) -> bool:
    if group in WANTED_GROUPS:
        return True
    upper = display.upper().strip()
    return any(upper.startswith(prefix) for prefix in WANTED_PREFIXES)


def to_12hr(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    h = hour % 12
    if h == 0:
        h = 12
    return f"{h}:{minute:02d} {suffix}"


def convert_hour(hour: int, ampm: str | None) -> int:
    if not ampm:
        return hour
    ampm = ampm.upper()
    if ampm == "PM" and hour != 12:
        return hour + 12
    if ampm == "AM" and hour == 12:
        return 0
    return hour


def infer_date(a: int, b: int, year_text: str | None, tz: ZoneInfo, now: datetime):
    """
    Vortex mostly uses MM-DD. For slash dates with a first value > 12,
    interpret as DD/MM. Handles 08-28, 08-28-26, 16/08, 8/28/2026.
    """
    if a > 12:
        day, month = a, b
    else:
        month, day = a, b

    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
    else:
        # Pick the nearest sensible year.
        candidates = []
        for year in (now.year - 1, now.year, now.year + 1):
            try:
                candidates.append(datetime(year, month, day, tzinfo=tz))
            except ValueError:
                pass
        if not candidates:
            return None
        year = min(candidates, key=lambda d: abs((d - now).total_seconds())).year

    try:
        return datetime(year, month, day, tzinfo=tz)
    except ValueError:
        return None


def parse_listing(display: str, timezone_name: str):
    """
    Returns:
      (start, stop, visible_title, has_explicit_date)

    Explicit-date listings are scheduled on the real date.
    No-date listings get a rolling 13-hour window around now.
    """
    if ":" not in display:
        return None

    payload = display.split(":", 1)[1].strip()
    if not payload:
        return None

    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)

    date_obj = None
    dm = DATE_RE.search(payload)
    if dm:
        date_obj = infer_date(
            int(dm.group("a")),
            int(dm.group("b")),
            dm.group("year"),
            tz,
            now,
        )
        if date_obj:
            payload_without_date = payload[:dm.start()].strip()
        else:
            payload_without_date = payload
    else:
        payload_without_date = payload

    tm = TIME_RE.match(payload_without_date)

    if tm:
        hour = convert_hour(int(tm.group("hour")), tm.group("ampm"))
        minute = int(tm.group("minute"))
        rest = tm.group("rest").strip().lstrip("|").strip()
        visible = f"{to_12hr(hour, minute)} {rest}".strip()

        if date_obj:
            start = datetime(
                date_obj.year, date_obj.month, date_obj.day,
                hour, minute, tzinfo=tz
            )
            stop = start + timedelta(hours=4)
            return start, stop, visible, True

        # No explicit date: show as the provider's current listing until refresh.
        start = now - timedelta(minutes=15)
        stop = now + timedelta(hours=13)
        return start, stop, visible, False

    # Has listing text but no parseable time.
    if date_obj:
        # Put it at noon on the explicit date rather than today.
        start = datetime(
            date_obj.year, date_obj.month, date_obj.day,
            12, 0, tzinfo=tz
        )
        stop = start + timedelta(hours=12)
        return start, stop, payload_without_date, True

    start = now - timedelta(minutes=15)
    stop = now + timedelta(hours=13)
    return start, stop, payload, False


def download_playlist() -> str:
    url = os.environ.get("XTREAM_M3U_URL", "").strip()
    if not url:
        raise RuntimeError("XTREAM_M3U_URL GitHub secret is missing.")

    req = Request(url, headers={"User-Agent": "TiviMate-M3U-EPG/2.2"})
    with urlopen(req, timeout=90) as response:
        text = response.read().decode("utf-8", errors="replace")

    if "#EXTM3U" not in text:
        raise RuntimeError("Vortex did not return a valid M3U playlist.")
    return text


def main():
    playlist = download_playlist()

    channels = []
    programmes = []
    seen_ids = set()
    group_counts = {}
    dated_counts = {}
    rolling_counts = {}

    for line in playlist.splitlines():
        if not line.startswith("#EXTINF:"):
            continue

        metadata = attrs(line)
        group = metadata.get("group-title", "").strip()
        display = (
            line.split(",", 1)[1].strip()
            if "," in line
            else metadata.get("tvg-name", "").strip()
        )

        if not wanted_channel(group, display):
            continue

        slot = stable_slot(display)
        channel_id = f"custom.{safe_id(group or 'VORTEX')}.{safe_id(slot)}"

        if channel_id not in seen_ids:
            seen_ids.add(channel_id)
            channels.append((channel_id, slot, display, group))
            group_counts[group] = group_counts.get(group, 0) + 1

        timezone_name = GROUP_TIMEZONES.get(group, "UTC")
        parsed = parse_listing(display, timezone_name)
        if not parsed:
            continue

        start, stop, title, is_dated = parsed
        programmes.append((channel_id, start, stop, title, group))

        if is_dated:
            dated_counts[group] = dated_counts.get(group, 0) + 1
        else:
            rolling_counts[group] = rolling_counts.get(group, 0) + 1

    output = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="Vortex Hybrid Date-Aware EPG v2.2">',
    ]

    for channel_id, slot, display, group in channels:
        output.append(f'  <channel id="{html.escape(channel_id, quote=True)}">')
        output.append(f'    <display-name>{html.escape(slot)}</display-name>')
        if display and display != slot:
            output.append(f'    <display-name>{html.escape(display)}</display-name>')
        if group:
            output.append(f'    <display-name>{html.escape(group)}</display-name>')
        output.append('  </channel>')

    for channel_id, start, stop, title, group in programmes:
        output.append(
            f'  <programme start="{start.strftime("%Y%m%d%H%M%S %z")}" '
            f'stop="{stop.strftime("%Y%m%d%H%M%S %z")}" '
            f'channel="{html.escape(channel_id, quote=True)}">'
        )
        output.append(f'    <title>{html.escape(title)}</title>')
        output.append('    <category>Sports</category>')
        output.append(
            f'    <desc>{html.escape(group or "Vortex")} • Vortex playlist listing</desc>'
        )
        output.append('  </programme>')

    output.append('</tv>')
    OUTPUT.write_text("\n".join(output) + "\n", encoding="utf-8")

    print(f"Generated {len(channels)} channels and {len(programmes)} programme listings.")
    for group in sorted(group_counts):
        print(
            f"{group or '(no group)'}: {group_counts[group]} channels, "
            f"{dated_counts.get(group, 0)} dated listings, "
            f"{rolling_counts.get(group, 0)} rolling listings"
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"EPG generation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
