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

# Vortex groups to include.
# Add another exact group-title here later if required.
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

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')

# Vortex event formats:
# PPV 04: 09:30 ACA 206 ...
# PARAMOUNT 003: 15:59 UFC 330: English
# SKY SPORTS+ 001: 09:25 | ATP Tour...
# AU STAN ALT 028: 00:00 ... 08-17
TIME_AT_START = re.compile(
    r'^\s*(?P<time>\d{1,2}:\d{2})(?:\s*(?P<ampm>AM|PM))?\s*(?:\|\s*)?(?P<title>.+?)\s*$',
    re.I,
)
DATE_MMDD = re.compile(r'\s+(?P<date>\d{2}-\d{2})\s*$')
DATE_DDMM = re.compile(r'\s+(?P<date>\d{1,2}/\d{1,2})\s*$')


def attrs(line):
    return dict(ATTR_RE.findall(line))


def safe_id(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", ".", value.strip())
    return value.strip(".") or "channel"


def stable_slot(display):
    return display.split(":", 1)[0].strip() if ":" in display else display.strip()


def xml_dt(dt):
    return dt.strftime("%Y%m%d%H%M%S %z")


def convert_hour(hour, ampm):
    if not ampm:
        return hour
    ampm = ampm.upper()
    if ampm == "PM" and hour != 12:
        return hour + 12
    if ampm == "AM" and hour == 12:
        return 0
    return hour


def closest_year(month, day, now):
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(datetime(year, month, day, tzinfo=now.tzinfo))
        except ValueError:
            pass
    return min(candidates, key=lambda d: abs((d - now).total_seconds())).year


def event_from_name(display, timezone_name):
    """Turn the live M3U channel name into an XMLTV programme."""
    if ":" not in display:
        return None

    payload = display.split(":", 1)[1].strip()
    if not payload:
        return None

    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)

    explicit_date = None

    m = DATE_MMDD.search(payload)
    if m:
        month, day = map(int, m.group("date").split("-"))
        year = closest_year(month, day, now)
        explicit_date = (year, month, day)
        payload = payload[:m.start()].strip()
    else:
        m = DATE_DDMM.search(payload)
        if m:
            day, month = map(int, m.group("date").split("/"))
            year = closest_year(month, day, now)
            explicit_date = (year, month, day)
            payload = payload[:m.start()].strip()

    tm = TIME_AT_START.match(payload)

    if tm:
        title = tm.group("title").strip().lstrip("|").strip()
        hour, minute = map(int, tm.group("time").split(":"))
        hour = convert_hour(hour, tm.group("ampm"))

        if explicit_date:
            year, month, day = explicit_date
            start = datetime(year, month, day, hour, minute, tzinfo=tz)
        else:
            today = datetime(now.year, now.month, now.day, hour, minute, tzinfo=tz)
            # Vortex often omits dates. Pick the nearest occurrence of this time.
            start = min(
                [today - timedelta(days=1), today, today + timedelta(days=1)],
                key=lambda d: abs((d - now).total_seconds()),
            )

        return start, start + timedelta(hours=4), title

    # If Vortex gives an event title but no parseable time, still put it
    # in the EPG as the current listing until the next refresh.
    title = payload.strip()
    if title:
        start = now - timedelta(hours=1)
        return start, start + timedelta(hours=13), title

    return None


def download_playlist():
    url = os.environ.get("XTREAM_M3U_URL", "").strip()
    if not url:
        raise RuntimeError("XTREAM_M3U_URL GitHub secret is missing.")

    req = Request(url, headers={"User-Agent": "TiviMate-M3U-EPG/2.0"})
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

    for line in playlist.splitlines():
        if not line.startswith("#EXTINF:"):
            continue

        metadata = attrs(line)
        group = metadata.get("group-title", "").strip()
        if group not in GROUP_TIMEZONES:
            continue

        display = line.split(",", 1)[1].strip() if "," in line else metadata.get("tvg-name", "").strip()
        slot = stable_slot(display)

        # Stable ID is based ONLY on group + slot, never the changing event title.
        channel_id = f"custom.{safe_id(group)}.{safe_id(slot)}"

        if channel_id not in seen_ids:
            seen_ids.add(channel_id)
            channels.append((channel_id, slot, display, group))

        event = event_from_name(display, GROUP_TIMEZONES[group])
        if event:
            start, stop, title = event
            programmes.append((channel_id, start, stop, title, group))
            group_counts[group] = group_counts.get(group, 0) + 1

    output = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="Vortex Live M3U Event EPG v2.0">',
    ]

    for channel_id, slot, display, group in channels:
        output.append(f'  <channel id="{html.escape(channel_id, quote=True)}">')
        output.append(f'    <display-name>{html.escape(slot)}</display-name>')
        # Also expose the provider's current full name for easier manual matching.
        if display != slot:
            output.append(f'    <display-name>{html.escape(display)}</display-name>')
        output.append(f'    <display-name>{html.escape(group)}</display-name>')
        output.append('  </channel>')

    for channel_id, start, stop, title, group in programmes:
        output.append(
            f'  <programme start="{xml_dt(start)}" stop="{xml_dt(stop)}" '
            f'channel="{html.escape(channel_id, quote=True)}">'
        )
        output.append(f'    <title>{html.escape(title)}</title>')
        output.append('    <category>Sports</category>')
        output.append(f'    <desc>{html.escape(group)} • Vortex live playlist listing</desc>')
        output.append('  </programme>')

    output.append('</tv>')
    OUTPUT.write_text("\n".join(output) + "\n", encoding="utf-8")

    print(f"Generated {len(channels)} EPG channels and {len(programmes)} programme listings.")
    for group in sorted(group_counts):
        print(f"{group}: {group_counts[group]} listings")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"EPG generation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
