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
STATE_FILE = Path("zulip_epg_state.json")
DISPLAY_TZ = ZoneInfo("Pacific/Auckland")

# ---------------------------------------------------------------------------
# ZULIP-MANAGED GROUPS
# These seven groups use Zulip as the schedule source and retain their last
# known Zulip schedule until a newer valid post is found.
# ---------------------------------------------------------------------------

ZULIP_SERVICES = {
    "dirtvision": {
        "channels": ["dirtvision"],
        "group": "DIRTVISION",
        "prefixes": ["DIRT"],
        "timezone": "America/New_York",
    },
    "paramount": {
        "channels": ["paramount"],
        "group": "US PARAMOUNT",
        "prefixes": ["PARAMOUNT"],
        "timezone": "America/New_York",
    },
    "pay-per-view": {
        "channels": ["pay-per-view"],
        "group": "PAY PER VIEW",
        "prefixes": ["PPV"],
        "timezone": "America/New_York",
    },
    "pay-per-view2": {
        "channels": ["pay-per-view2", "pay-per-view-2"],
        "group": "PAY PER VIEW 2",
        "prefixes": ["PPV2"],
        "timezone": "America/New_York",
    },
    "sky-sports-plus": {
        "channels": ["sky-sports-plus"],
        "group": "SKY SPORTS+",
        "prefixes": ["SKY SPORTS+"],
        "timezone": "Europe/London",
    },
    "stan-sport": {
        "channels": ["stan-sport"],
        "group": "AU STAN",
        "prefixes": ["AU STAN ALT", "AU STAN"],
        "timezone": "Australia/Sydney",
    },
    "discovery-plus": {
        "channels": ["discovery-plus"],
        "group": "UK DISCOVERY+",
        "prefixes": ["UK D+"],
        "timezone": "Europe/London",
    },
}

ZULIP_MANAGED_GROUPS = {svc["group"] for svc in ZULIP_SERVICES.values()}

# ---------------------------------------------------------------------------
# M3U-MANAGED GROUPS
# Everything here keeps the working "fresh channel name -> EPG" method.
# ---------------------------------------------------------------------------

M3U_GROUP_TIMEZONES = {
    "AU KAYO+": "Australia/Sydney",
    "US ESPN+": "America/New_York",
    "UFC": "America/New_York",
    "CA SPORTSNET+": "America/Toronto",
    "CA TSN+": "America/Toronto",
    "ESPN PLAY": "America/New_York",
}

M3U_PREFIXES = (
    "KAYO+ ",
    "AU KAYO+ ",
    "ESPN+ ",
    "ESPN+ ALT ",
    "ESPN+ ALT2 ",
    "UFC ",
    "SPORTSNET+ ",
    "SN+ ",
    "TSN+ ",
    "ESPNPLAY ",
)

# ---------------------------------------------------------------------------

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
SCHEDULE_LINE_RE = re.compile(
    r'^\s*(?P<slot>[^:]{1,100}?\d{1,4})\s*:\s*(?P<body>.*?)\s*$',
    re.I,
)
TIME_RE = re.compile(
    r'^\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})'
    r'(?:\s*(?P<ampm>AM|PM))?'
    r'\s*(?:\|\s*)?(?P<title>.+?)\s*$',
    re.I,
)
DATE_RE = re.compile(
    r'(?:\s+|^)'
    r'(?P<a>\d{1,2})[-/](?P<b>\d{1,2})'
    r'(?:[-/](?P<year>\d{2,4}))?'
    r'\s*$'
)

MONTHS = {
    "JAN":1,"JANUARY":1,"FEB":2,"FEBRUARY":2,"MAR":3,"MARCH":3,
    "APR":4,"APRIL":4,"MAY":5,"JUN":6,"JUNE":6,"JUL":7,"JULY":7,
    "AUG":8,"AUGUST":8,"SEP":9,"SEPT":9,"SEPTEMBER":9,
    "OCT":10,"OCTOBER":10,"NOV":11,"NOVEMBER":11,
    "DEC":12,"DECEMBER":12,
}
HEADER_RE = re.compile(
    r'\b(' + "|".join(sorted(map(re.escape, MONTHS), key=len, reverse=True)) +
    r')\s+(\d{1,2})(?:ST|ND|RD|TH)?\b',
    re.I,
)

def attrs(line):
    return dict(ATTR_RE.findall(line))

def safe_id(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", ".", value.strip())
    return value.strip(".") or "channel"

def normalize_prefix(prefix):
    return re.sub(r"\s+", " ", prefix.strip()).upper()

def normalize_slot(name):
    name = re.sub(r"\s+", " ", name.strip()).upper()
    m = re.match(r"^(.*?\D)(\d{1,4})$", name)
    if not m:
        return name
    prefix = re.sub(r"\s+", " ", m.group(1).strip())
    return f"{prefix} {int(m.group(2))}"

def stable_slot(display):
    return display.split(":", 1)[0].strip() if ":" in display else display.strip()

def to_12hr(dt):
    suffix = "AM" if dt.hour < 12 else "PM"
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {suffix}"

def convert_hour(hour, ampm):
    if not ampm:
        return hour
    a = ampm.upper()
    if a == "PM" and hour != 12:
        return hour + 12
    if a == "AM" and hour == 12:
        return 0
    return hour

def closest_year(month, day, tz, reference):
    candidates = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(datetime(year, month, day, tzinfo=tz))
        except ValueError:
            pass
    if not candidates:
        raise ValueError("Invalid date")
    return min(candidates, key=lambda d: abs((d-reference).total_seconds())).year

def infer_numeric_date(a, b, year_text, tz, reference):
    # Vortex usually uses MM-DD. If first number cannot be a month, use DD/MM.
    if a > 12:
        day, month = a, b
    else:
        month, day = a, b

    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
    else:
        try:
            year = closest_year(month, day, tz, reference)
        except ValueError:
            return None

    try:
        return datetime(year, month, day, tzinfo=tz)
    except ValueError:
        return None

def header_date(content, tz, msg_local):
    m = HEADER_RE.search(content)
    if not m:
        return None
    month = MONTHS[m.group(1).upper()]
    day = int(m.group(2))
    year = closest_year(month, day, tz, msg_local)
    return datetime(year, month, day, tzinfo=tz)

# ---------------------------------------------------------------------------
# ZULIP
# ---------------------------------------------------------------------------

def zulip_messages(channel):
    site = os.environ["ZULIP_SITE"].rstrip("/")
    email = os.environ["ZULIP_EMAIL"].strip()
    api_key = os.environ["ZULIP_API_KEY"].strip()

    token = base64.b64encode(f"{email}:{api_key}".encode()).decode()

    query = urlencode({
        "anchor": "newest",
        "num_before": 100,
        "num_after": 0,
        "apply_markdown": "false",
        "narrow": json.dumps([{"operator": "channel", "operand": channel}]),
    })

    req = Request(
        f"{site}/api/v1/messages?{query}",
        headers={
            "Authorization": f"Basic {token}",
            "User-Agent": "TiviMate-Hybrid-EPG/3.1",
        },
    )

    with urlopen(req, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("result") != "success":
        raise RuntimeError(data.get("msg", "Zulip API error"))

    return data.get("messages", [])

def raw_content(message):
    return str(message.get("content") or message.get("raw_content") or "")

def content_has_schedule(content, prefixes):
    for line in content.splitlines():
        m = SCHEDULE_LINE_RE.match(line)
        if not m:
            continue
        slot_norm = normalize_slot(m.group("slot"))
        if any(slot_norm.startswith(normalize_prefix(p)) for p in prefixes):
            return True
    return False

def newest_schedule(service):
    best = None

    for channel in service["channels"]:
        try:
            messages = zulip_messages(channel)
        except Exception as exc:
            print(f"  #{channel}: unavailable ({exc})")
            continue

        for message in messages:
            content = raw_content(message)
            if not content_has_schedule(content, service["prefixes"]):
                continue

            timestamp = int(message.get("timestamp", 0))
            if best is None or timestamp > int(best[0].get("timestamp", 0)):
                best = (message, channel)

    return best

def parse_zulip_message(content, service, timestamp):
    tz = ZoneInfo(service["timezone"])
    msg_local = datetime.fromtimestamp(timestamp, ZoneInfo("UTC")).astimezone(tz)
    base_date = header_date(content, tz, msg_local)

    # Stan posts sometimes omit a header date.
    if base_date is None:
        base_date = datetime(msg_local.year, msg_local.month, msg_local.day, tzinfo=tz)

    events = []
    previous_start = None
    rollover_days = 0

    for raw_line in content.splitlines():
        m = SCHEDULE_LINE_RE.match(raw_line)
        if not m:
            continue

        slot = re.sub(r"\s+", " ", m.group("slot").strip())
        slot_norm = normalize_slot(slot)

        if not any(
            slot_norm.startswith(normalize_prefix(prefix))
            for prefix in service["prefixes"]
        ):
            continue

        body = m.group("body").strip()
        if not body:
            continue

        explicit_date = None
        dm = DATE_RE.search(body)
        if dm:
            explicit_date = infer_numeric_date(
                int(dm.group("a")),
                int(dm.group("b")),
                dm.group("year"),
                tz,
                msg_local,
            )
            if explicit_date:
                body = body[:dm.start()].strip()

        tm = TIME_RE.match(body)

        if tm:
            hour = convert_hour(int(tm.group("hour")), tm.group("ampm"))
            minute = int(tm.group("minute"))
            title = tm.group("title").strip().lstrip("|").strip()

            if explicit_date:
                start = explicit_date.replace(hour=hour, minute=minute)
            else:
                candidate = (base_date + timedelta(days=rollover_days)).replace(
                    hour=hour, minute=minute
                )

                # If the schedule crosses midnight, move later low-hour entries
                # to the following day.
                if previous_start and candidate < previous_start - timedelta(hours=8):
                    rollover_days += 1
                    candidate = (base_date + timedelta(days=rollover_days)).replace(
                        hour=hour, minute=minute
                    )

                start = candidate

            stop = start + timedelta(hours=4)

        else:
            # No time: retain it as a daytime block.
            start = (explicit_date or (base_date + timedelta(days=rollover_days))).replace(
                hour=12, minute=0
            )
            stop = start + timedelta(hours=12)
            title = body

        previous_start = start

        local_start = start.astimezone(DISPLAY_TZ)

        events.append({
            "slot": slot,
            "slot_norm": slot_norm,
            "title": title,
            "display_title": f"{to_12hr(local_start)} {title}",
            "start": start.isoformat(),
            "stop": stop.isoformat(),
        })

    return events

def load_state():
    if not STATE_FILE.exists():
        return {"version": 1, "services": {}}

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("version", 1)
        data.setdefault("services", {})
        return data
    except Exception:
        return {"version": 1, "services": {}}

def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

def update_zulip_state(state):
    for key, service in ZULIP_SERVICES.items():
        print(f"Checking Zulip: {key}")

        latest = newest_schedule(service)

        if latest is None:
            previous = state["services"].get(key)
            if previous:
                print(f"  no fresh schedule; retaining {len(previous.get('events', []))} saved events")
            else:
                print("  no schedule found yet")
            continue

        message, channel = latest
        timestamp = int(message.get("timestamp", 0))
        message_id = int(message.get("id", 0))
        previous = state["services"].get(key, {})

        if previous and timestamp <= int(previous.get("message_timestamp", 0)):
            print(f"  no newer post; retaining {len(previous.get('events', []))} saved events")
            continue

        events = parse_zulip_message(raw_content(message), service, timestamp)

        if not events:
            print("  newer post could not be parsed; retaining old schedule")
            continue

        state["services"][key] = {
            "message_id": message_id,
            "message_timestamp": timestamp,
            "channel": channel,
            "group": service["group"],
            "timezone": service["timezone"],
            "events": events,
            "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }

        print(f"  SAVED NEW SCHEDULE from #{channel}: {len(events)} events")

# ---------------------------------------------------------------------------
# M3U
# ---------------------------------------------------------------------------

def download_m3u():
    url = os.environ["XTREAM_M3U_URL"].strip()

    req = Request(
        url,
        headers={"User-Agent": "TiviMate-Hybrid-EPG/3.1"},
    )

    with urlopen(req, timeout=90) as response:
        text = response.read().decode("utf-8", errors="replace")

    if "#EXTM3U" not in text:
        raise RuntimeError("Xtream URL did not return a valid M3U")

    return text

def m3u_managed_channel(group, display):
    if group in M3U_GROUP_TIMEZONES:
        return True

    upper = display.upper().strip()
    return any(upper.startswith(prefix) for prefix in M3U_PREFIXES)

def parse_m3u_listing(display, timezone_name):
    """
    Working v2.3-style behaviour:
    - explicit dates are scheduled on their real source date/time
    - no-date listings stay visible until the next refresh
    - visible title time is converted to NZ local time
    """
    if ":" not in display:
        return None

    payload = display.split(":", 1)[1].strip()
    if not payload:
        return None

    source_tz = ZoneInfo(timezone_name)
    now_source = datetime.now(source_tz)

    explicit_date = None
    dm = DATE_RE.search(payload)

    if dm:
        explicit_date = infer_numeric_date(
            int(dm.group("a")),
            int(dm.group("b")),
            dm.group("year"),
            source_tz,
            now_source,
        )
        if explicit_date:
            payload_without_date = payload[:dm.start()].strip()
        else:
            payload_without_date = payload
    else:
        payload_without_date = payload

    tm = TIME_RE.match(payload_without_date)

    if tm:
        hour = convert_hour(int(tm.group("hour")), tm.group("ampm"))
        minute = int(tm.group("minute"))
        title = tm.group("title").strip().lstrip("|").strip()

        if explicit_date:
            start = explicit_date.replace(hour=hour, minute=minute)
            stop = start + timedelta(hours=4)
            local = start.astimezone(DISPLAY_TZ)
            return start, stop, f"{to_12hr(local)} {title}"

        # No explicit date: keep as current listing until next refresh.
        candidate = datetime(
            now_source.year, now_source.month, now_source.day,
            hour, minute, tzinfo=source_tz
        )

        candidate = min(
            [candidate - timedelta(days=1), candidate, candidate + timedelta(days=1)],
            key=lambda d: abs((d - now_source).total_seconds()),
        )

        local = candidate.astimezone(DISPLAY_TZ)

        start = now_source - timedelta(minutes=15)
        stop = now_source + timedelta(hours=3)

        return start, stop, f"{to_12hr(local)} {title}"

    if explicit_date:
        start = explicit_date.replace(hour=12, minute=0)
        return start, start + timedelta(hours=12), payload_without_date

    return (
        now_source - timedelta(minutes=15),
        now_source + timedelta(hours=3),
        payload,
    )

def build_m3u_data(m3u):
    channels = []
    lookup = {}
    m3u_programmes = []
    seen = set()

    all_relevant_groups = ZULIP_MANAGED_GROUPS | set(M3U_GROUP_TIMEZONES)

    for line in m3u.splitlines():
        if not line.startswith("#EXTINF:"):
            continue

        meta = attrs(line)
        group = meta.get("group-title", "").strip()
        display = (
            line.split(",", 1)[1].strip()
            if "," in line
            else meta.get("tvg-name", "").strip()
        )

        # Keep channels for both Zulip- and M3U-managed groups.
        if group not in all_relevant_groups and not m3u_managed_channel(group, display):
            continue

        slot = stable_slot(display)
        slot_norm = normalize_slot(slot)
        channel_id = f"custom.{safe_id(group or 'VORTEX')}.{safe_id(slot)}"

        if channel_id not in seen:
            seen.add(channel_id)
            channel = {
                "id": channel_id,
                "slot": slot,
                "slot_norm": slot_norm,
                "display": display,
                "group": group,
            }
            channels.append(channel)
            lookup[(group, slot_norm)] = channel

        # Programme data from M3U only for non-Zulip-managed groups.
        if group in ZULIP_MANAGED_GROUPS:
            continue

        if not m3u_managed_channel(group, display):
            continue

        timezone_name = M3U_GROUP_TIMEZONES.get(group, "UTC")
        parsed = parse_m3u_listing(display, timezone_name)

        if not parsed:
            continue

        start, stop, title = parsed

        m3u_programmes.append({
            "channel_id": channel_id,
            "start": start.isoformat(),
            "stop": stop.isoformat(),
            "title": title,
            "source": "Vortex M3U",
            "group": group,
        })

    return channels, lookup, m3u_programmes

# ---------------------------------------------------------------------------
# XML OUTPUT
# ---------------------------------------------------------------------------

def write_xml(state, channels, lookup, m3u_programmes):
    channel_by_id = {c["id"]: c for c in channels}
    programmes = list(m3u_programmes)

    # Add remembered Zulip schedules.
    for service_key, saved in state.get("services", {}).items():
        group = saved.get("group", "")

        for event in saved.get("events", []):
            ch = lookup.get((group, event["slot_norm"]))

            if ch is None:
                # Keep a virtual EPG channel so remembered schedules survive
                # even if the M3U temporarily omits the slot.
                channel_id = f"custom.{safe_id(group)}.{safe_id(event['slot'])}"
                ch = {
                    "id": channel_id,
                    "slot": event["slot"],
                    "slot_norm": event["slot_norm"],
                    "display": event["slot"],
                    "group": group,
                }
                channel_by_id[channel_id] = ch

            programmes.append({
                "channel_id": ch["id"],
                "start": event["start"],
                "stop": event["stop"],
                "title": event["display_title"],
                "source": f"Zulip #{service_key}",
                "group": group,
            })

    output = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="Vortex Hybrid Zulip + M3U EPG v3.1">',
    ]

    for ch in channel_by_id.values():
        output.append(f'  <channel id="{html.escape(ch["id"], quote=True)}">')
        output.append(f'    <display-name>{html.escape(ch["slot"])}</display-name>')
        if ch.get("display") and ch["display"] != ch["slot"]:
            output.append(f'    <display-name>{html.escape(ch["display"])}</display-name>')
        output.append(f'    <display-name>{html.escape(ch["group"])}</display-name>')
        output.append('  </channel>')

    programmes.sort(key=lambda p: (p["channel_id"], p["start"]))

    for p in programmes:
        start = datetime.fromisoformat(p["start"]).strftime("%Y%m%d%H%M%S %z")
        stop = datetime.fromisoformat(p["stop"]).strftime("%Y%m%d%H%M%S %z")

        output.append(
            f'  <programme start="{start}" stop="{stop}" '
            f'channel="{html.escape(p["channel_id"], quote=True)}">'
        )
        output.append(f'    <title>{html.escape(p["title"])}</title>')
        output.append('    <category>Sports</category>')
        output.append(
            f'    <desc>{html.escape(p["source"])} • {html.escape(p["group"])}</desc>'
        )
        output.append('  </programme>')

    output.append('</tv>')

    OUTPUT.write_text("\n".join(output) + "\n", encoding="utf-8")

    print(
        f"Wrote {len(channel_by_id)} channels, "
        f"{len(programmes)} total programme entries."
    )
    print(
        f"  Zulip-managed groups: {len(ZULIP_MANAGED_GROUPS)}"
    )
    print(
        f"  M3U-managed groups: {len(M3U_GROUP_TIMEZONES)}"
    )

def main():
    state = load_state()
    update_zulip_state(state)
    save_state(state)

    m3u = download_m3u()
    channels, lookup, m3u_programmes = build_m3u_data(m3u)

    write_xml(state, channels, lookup, m3u_programmes)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"EPG generation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
