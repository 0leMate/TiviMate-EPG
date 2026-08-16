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

# Exact Vortex groups currently known from the playlist.
WANTED_GROUPS = {
    "PAY PER VIEW",
    "PAY PER VIEW 2",
    "US PARAMOUNT",
    "AU STAN",
    "AU KAYO+",
    "US ESPN+",
    "DIRTVISION",
    "UFC",
    "CA SPORTSNET+",
    "CA TSN+",
    "SKY SPORTS+",
    "UK DISCOVERY+",
    "ESPN PLAY",
}

# Also accept channels by prefix even if Vortex moves them to another group later.
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
    "TSN+ ",
    "UFC ",
    "UK D+ ",
)

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def attrs(line: str) -> dict[str, str]:
    return dict(ATTR_RE.findall(line))


def safe_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", ".", value.strip())
    return value.strip(".") or "channel"


def stable_slot(display: str) -> str:
    return display.split(":", 1)[0].strip() if ":" in display else display.strip()


def current_listing(display: str) -> str:
    """
    Use exactly what Vortex currently publishes after the stable slot.
    This intentionally keeps embedded time/date text in the EPG title.
    """
    if ":" not in display:
        return ""
    return display.split(":", 1)[1].strip()


def wanted_channel(group: str, display: str) -> bool:
    if group in WANTED_GROUPS:
        return True

    upper = display.upper().strip()
    return any(upper.startswith(prefix) for prefix in WANTED_PREFIXES)


def download_playlist() -> str:
    url = os.environ.get("XTREAM_M3U_URL", "").strip()
    if not url:
        raise RuntimeError("XTREAM_M3U_URL GitHub secret is missing.")

    req = Request(url, headers={"User-Agent": "TiviMate-M3U-EPG/2.1"})
    with urlopen(req, timeout=90) as response:
        text = response.read().decode("utf-8", errors="replace")

    if "#EXTM3U" not in text:
        raise RuntimeError("Vortex did not return a valid M3U playlist.")
    return text


def main():
    playlist = download_playlist()

    # Make the current provider listing visible continuously until the next
    # scheduled refresh. GitHub runs every 12 hours; 13 hours gives overlap.
    now = datetime.now(ZoneInfo("UTC"))
    start = now - timedelta(minutes=15)
    stop = now + timedelta(hours=13)

    channels = []
    programmes = []
    seen_ids = set()
    group_counts = {}
    listing_counts = {}
    samples = {}

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
        # Group + stable slot keeps TiviMate mapping stable as event names change.
        channel_id = f"custom.{safe_id(group or 'VORTEX')}.{safe_id(slot)}"

        if channel_id not in seen_ids:
            seen_ids.add(channel_id)
            channels.append((channel_id, slot, display, group))
            group_counts[group] = group_counts.get(group, 0) + 1
            samples.setdefault(group, [])
            if len(samples[group]) < 3:
                samples[group].append(display)

        listing = current_listing(display)
        if listing:
            programmes.append((channel_id, start, stop, listing, group))
            listing_counts[group] = listing_counts.get(group, 0) + 1

    output = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="Vortex Live Channel-Name EPG v2.1">',
    ]

    for channel_id, slot, display, group in channels:
        output.append(f'  <channel id="{html.escape(channel_id, quote=True)}">')
        output.append(f'    <display-name>{html.escape(slot)}</display-name>')
        if display and display != slot:
            output.append(f'    <display-name>{html.escape(display)}</display-name>')
        if group:
            output.append(f'    <display-name>{html.escape(group)}</display-name>')
        output.append('  </channel>')

    for channel_id, pstart, pstop, title, group in programmes:
        output.append(
            f'  <programme start="{pstart.strftime("%Y%m%d%H%M%S %z")}" '
            f'stop="{pstop.strftime("%Y%m%d%H%M%S %z")}" '
            f'channel="{html.escape(channel_id, quote=True)}">'
        )
        output.append(f'    <title>{html.escape(title)}</title>')
        output.append('    <category>Sports</category>')
        output.append(
            f'    <desc>{html.escape(group or "Vortex")} • current Vortex channel listing</desc>'
        )
        output.append('  </programme>')

    output.append('</tv>')
    OUTPUT.write_text("\n".join(output) + "\n", encoding="utf-8")

    print(f"Generated {len(channels)} channels and {len(programmes)} visible listings.")
    for group in sorted(group_counts):
        print(
            f"{group or '(no group)'}: "
            f"{group_counts[group]} channels, "
            f"{listing_counts.get(group, 0)} with current listing text"
        )
        for sample in samples.get(group, []):
            print(f"  sample: {sample}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"EPG generation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
