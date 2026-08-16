#!/usr/bin/env python3
from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

OUTPUT = Path("vortex_custom_event_epg.xml")

# Existing playlist groups we want represented in the custom EPG.
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

# Zulip channel -> schedule settings.
# "prefixes" are used to match a Zulip slot to the same slot in the M3U.
ZULIP_SERVICES = {
    "dirtvision": {
        "timezone": "America/New_York",
        "prefixes": ["DIRT"],
    },
    "paramount": {
        "timezone": "America/New_York",
        "prefixes": ["PARAMOUNT"],
    },
    "pay-per-view": {
        "timezone": "America/New_York",
        "prefixes": ["PPV"],
    },
    "pay-per-view2": {
        "timezone": "America/New_York",
        "prefixes": ["PPV2"],
    },
    "sky-sports-plus": {
        "timezone": "Europe/London",
        "prefixes": ["SKY SPORTS+"],
    },
    "stan-sport": {
        "timezone": "Australia/Sydney",
        "prefixes": ["AU STAN ALT", "AU STAN"],
    },
    "discovery-plus": {
        "timezone": "Europe/London",
        "prefixes": ["UK D+"],
    },
    "espn-play": {
        "timezone": "America/New_York",
        "prefixes": ["ESPNPLAY"],
    },
    "espn-plus": {
        "timezone": "America/New_York",
        "prefixes": ["ESPN+ ALT2", "ESPN+ ALT", "ESPN+"],
    },
}

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
DATE_MMDD_RE = re.compile(r'(?P<date>\b\d{2}-\d{2}\b)\s*$')
DATE_DDMM_RE = re.compile(r'(?P<date>\b\d{1,2}/\d{1,2}\b)\s*$')
TIME_FRONT_RE = re.compile(
    r'^\s*(?P<time>\d{1,2}:\d{2})(?:\s*(?P<ampm>AM|PM))?\s*(?:\|\s*)?(?P<title>.*?)\s*$',
    re.IGNORECASE,
)
TIME_END_RE = re.compile(
    r'^\s*(?P<title>.+?)\s+(?P<time>\d{1,2}:\d{2})(?:\s*(?P<ampm>AM|PM))?\s*$',
    re.IGNORECASE,
)

# Handles entries such as:
# SKY SPORTS+ 001: 09:25 | ATP Tour...
# PARAMOUNT 001: 14:35 Venezia vs Modena
# AU STAN ALT 017: 12:00 Southland vs Waikato...
SCHEDULE_LINE_RE = re.compile(
    r'^\s*(?P<slot>[^:]{2,80}?\s+\d{1,4})\s*:\s*(?P<body>.+?)\s*$',
    re.IGNORECASE,
)

MONTHS = {
    "JAN": 1, "JANUARY": 1,
    "FEB": 2, "FEBRUARY": 2,
    "MAR": 3, "MARCH": 3,
    "APR": 4, "APRIL": 4,
    "MAY": 5,
    "JUN": 6, "JUNE": 6,
    "JUL": 7, "JULY": 7,
    "AUG": 8, "AUGUST": 8,
    "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
    "OCT": 10, "OCTOBER": 10,
    "NOV": 11, "NOVEMBER": 11,
    "DEC": 12, "DECEMBER": 12,
}


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


def closest_year(month: int, day: int, tz: ZoneInfo, reference: datetime | None = None) -> int:
    now = reference.astimezone(tz) if reference else datetime.now(tz)
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(datetime(year, month, day, tzinfo=tz))
        except ValueError:
            pass
    return min(candidates, key=lambda d: abs((d - now).total_seconds())).year


def normalize_slot(name: str) -> str:
    """
    Normalises slot numbers so e.g. SKY SPORTS+ 01 and SKY SPORTS+ 001
    are treated as the same channel.
    """
    name = re.sub(r"\s+", " ", name.strip()).upper()
    m = re.match(r"^(.*?\D)(\d{1,4})$", name)
    if not m:
        return name
    prefix = re.sub(r"\s+", " ", m.group(1).strip())
    number = int(m.group(2))
    return f"{prefix} {number}"


def parse_header_date(text: str, tz: ZoneInfo, message_time: datetime) -> datetime | None:
    # Examples:
    # PARAMOUNT - AUG 15TH - ET
    # SKY SPORTS+ - FEB 27TH - GMT
    m = re.search(
        r'\b(' + "|".join(map(re.escape, MONTHS.keys())) + r')\s+(\d{1,2})(?:ST|ND|RD|TH)?\b',
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    month = MONTHS[m.group(1).upper()]
    day = int(m.group(2))
    year = closest_year(month, day, tz, message_time)
    return datetime(year, month, day, tzinfo=tz)


def parse_event_body(
    body: str,
    timezone_name: str,
    default_date: datetime | None = None,
    message_time: datetime | None = None,
):
    tz = ZoneInfo(timezone_name)
    now = message_time.astimezone(tz) if message_time else datetime.now(tz)

    payload = body.strip().replace("\\`", "`")
    explicit_date = None

    m = DATE_MMDD_RE.search(payload)
    if m:
        month, day = map(int, m.group("date").split("-"))
        year = closest_year(month, day, tz, now)
        explicit_date = datetime(year, month, day, tzinfo=tz)
        payload = payload[:m.start()].strip()
    else:
        m = DATE_DDMM_RE.search(payload)
        if m:
            day, month = map(int, m.group("date").split("/"))
            year = closest_year(month, day, tz, now)
            explicit_date = datetime(year, month, day, tzinfo=tz)
            payload = payload[:m.start()].strip()

    m = TIME_FRONT_RE.match(payload)
    if not m or not m.group("title").strip():
        m = TIME_END_RE.match(payload)

    if not m:
        # Useful current event text with no time.
        start = now - timedelta(hours=1)
        stop = now + timedelta(hours=12)
        return start, stop, payload

    title = m.group("title").strip().lstrip("|").strip()
    hour, minute = map(int, m.group("time").split(":"))
    hour = convert_hour(hour, m.group("ampm"))

    date_base = explicit_date or default_date
    if date_base:
        start = datetime(
            date_base.year, date_base.month, date_base.day,
            hour, minute, tzinfo=tz
        )
    else:
        start = datetime(now.year, now.month, now.day, hour, minute, tzinfo=tz)
        candidates = [start - timedelta(days=1), start, start + timedelta(days=1)]
        start = min(candidates, key=lambda d: abs((d - now).total_seconds()))

    return start, start + timedelta(hours=4), title


def basic_auth_header(email: str, api_key: str) -> str:
    token = base64.b64encode(f"{email}:{api_key}".encode()).decode()
    return f"Basic {token}"


def http_json(url: str, email: str, api_key: str) -> dict:
    req = Request(
        url,
        headers={
            "Authorization": basic_auth_header(email, api_key),
            "User-Agent": "TiviMate-EPG-Updater/1.2",
        },
    )
    with urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_zulip_messages(channel: str) -> list[dict]:
    site = os.environ.get("ZULIP_SITE", "").rstrip("/")
    email = os.environ.get("ZULIP_EMAIL", "")
    api_key = os.environ.get("ZULIP_API_KEY", "")
    if not (site and email and api_key):
        return []

    query = urlencode({
        "anchor": "newest",
        "num_before": 100,
        "num_after": 0,
        "apply_markdown": "false",
        "narrow": json.dumps([
            {"operator": "channel", "operand": channel},
        ]),
    })
    data = http_json(f"{site}/api/v1/messages?{query}", email, api_key)
    if data.get("result") != "success":
        raise RuntimeError(f"Zulip API error for #{channel}: {data}")
    return data.get("messages", [])


def choose_latest_schedule_message(channel: str, settings: dict) -> tuple[str, datetime] | None:
    messages = fetch_zulip_messages(channel)
    prefixes = [p.upper() for p in settings["prefixes"]]

    # Newest first.
    for message in reversed(messages):
        raw = str(message.get("content", ""))
        upper = raw.upper()
        if any(re.search(rf'(?m)^\s*{re.escape(prefix)}\s+\d+\s*:', upper)
               for prefix in prefixes):
            ts = datetime.fromtimestamp(int(message["timestamp"]), ZoneInfo("UTC"))
            return raw, ts
    return None


def download_playlist() -> str:
    url = os.environ.get("XTREAM_M3U_URL", "").strip()
    if not url:
        raise RuntimeError("XTREAM_M3U_URL repository secret is missing.")

    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 TiviMate-EPG-Updater/1.2"},
    )
    with urlopen(request, timeout=90) as response:
        text = response.read().decode("utf-8", errors="replace")

    if "#EXTM3U" not in text:
        raise RuntimeError("The downloaded response was not an M3U playlist.")
    return text


def build_m3u_data(playlist: str):
    channels = []
    programmes = []
    slot_lookup = {}

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

        channel = {
            "id": channel_id,
            "base": base,
            "display": display,
            "group": group,
        }
        channels.append(channel)
        slot_lookup.setdefault(normalize_slot(base), channel)

        # M3U schedule fallback.
        if ":" in display:
            body = display.split(":", 1)[1].strip()
            if body:
                parsed = parse_event_body(body, GROUP_TIMEZONES[group])
                if parsed:
                    start, stop, title = parsed
                    programmes.append({
                        "channel_id": channel_id,
                        "start": start,
                        "stop": stop,
                        "title": title,
                        "source": "M3U",
                        "group": group,
                    })

    # de-duplicate
    unique = []
    seen = set()
    for ch in channels:
        if ch["id"] not in seen:
            seen.add(ch["id"])
            unique.append(ch)
    return unique, programmes, slot_lookup


def apply_zulip_overlays(channels, programmes, slot_lookup):
    # Replace M3U programme for any exact slot for which Zulip supplies a listing.
    by_channel = {p["channel_id"]: p for p in programmes}

    for channel_name, settings in ZULIP_SERVICES.items():
        try:
            latest = choose_latest_schedule_message(channel_name, settings)
        except Exception as exc:
            print(f"WARNING: Zulip #{channel_name} fetch failed: {exc}", file=sys.stderr)
            continue

        if not latest:
            print(f"Zulip #{channel_name}: no schedule message found.")
            continue

        content, message_time = latest
        tz = ZoneInfo(settings["timezone"])
        header_date = parse_header_date(content, tz, message_time)
        count = 0

        for raw_line in content.splitlines():
            line = raw_line.strip()
            m = SCHEDULE_LINE_RE.match(line)
            if not m:
                continue

            slot = re.sub(r"\s+", " ", m.group("slot").strip())
            slot_upper = slot.upper()
            if not any(slot_upper.startswith(prefix.upper()) for prefix in settings["prefixes"]):
                continue

            body = m.group("body").strip()
            parsed = parse_event_body(
                body,
                settings["timezone"],
                default_date=header_date,
                message_time=message_time,
            )
            if not parsed:
                continue

            key = normalize_slot(slot)
            channel = slot_lookup.get(key)

            if channel is None:
                # Zulip-only service/slot. Keep it in XML so it is available for
                # manual assignment in TiviMate if the playlist has a matching channel.
                channel_id = f"custom.ZULIP.{safe_id(slot)}"
                channel = {
                    "id": channel_id,
                    "base": slot,
                    "display": slot,
                    "group": f"ZULIP {channel_name}",
                }
                channels.append(channel)
                slot_lookup[key] = channel

            start, stop, title = parsed
            by_channel[channel["id"]] = {
                "channel_id": channel["id"],
                "start": start,
                "stop": stop,
                "title": title,
                "source": f"Zulip #{channel_name}",
                "group": channel["group"],
            }
            count += 1

        print(f"Zulip #{channel_name}: applied {count} listings.")

    return list(by_channel.values())


def write_xml(channels, programmes):
    # de-duplicate any virtual channels
    unique = []
    seen = set()
    for ch in channels:
        if ch["id"] not in seen:
            seen.add(ch["id"])
            unique.append(ch)

    output = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="Automatic Vortex + Zulip Event EPG v1.2">',
    ]

    for ch in unique:
        output.append(f'  <channel id="{html.escape(ch["id"], quote=True)}">')
        output.append(f'    <display-name>{html.escape(ch["base"])}</display-name>')
        if ch["display"] and ch["display"] != ch["base"]:
            output.append(f'    <display-name>{html.escape(ch["display"])}</display-name>')
        output.append(f'    <display-name>{html.escape(ch["group"])}</display-name>')
        output.append("  </channel>")

    for p in sorted(programmes, key=lambda x: (x["channel_id"], x["start"])):
        output.append(
            f'  <programme start="{xmltv_dt(p["start"])}" '
            f'stop="{xmltv_dt(p["stop"])}" '
            f'channel="{html.escape(p["channel_id"], quote=True)}">'
        )
        output.append(f'    <title>{html.escape(p["title"])}</title>')
        output.append("    <category>Sports</category>")
        output.append(
            f'    <desc>{html.escape(p["source"])} • {html.escape(p["group"])}</desc>'
        )
        output.append("  </programme>")

    output.append("</tv>")
    OUTPUT.write_text("\n".join(output) + "\n", encoding="utf-8")
    print(f"Generated {OUTPUT}: {len(unique)} channels, {len(programmes)} programmes.")


def main():
    playlist = download_playlist()
    channels, programmes, slot_lookup = build_m3u_data(playlist)
    programmes = apply_zulip_overlays(channels, programmes, slot_lookup)
    write_xml(channels, programmes)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"EPG generation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
