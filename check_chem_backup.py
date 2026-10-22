#!/usr/bin/env python3
"""
Single-shot availability check, designed to run on GitHub Actions.

The long-running local watcher (greep_watch.py) polls every 20 seconds. This
one runs once per invocation and exits, because Actions bills per run.

Every identifier lives in an environment variable fed from repository secrets,
so this file stays generic enough to sit in a public repo.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = "https://bookings.cloud.microsoft/BookingsService/api/V1/bookingBusinessesc2"
STATE_FILE = Path(__file__).resolve().parent / "state.json"
# The endpoint has been seen taking 17s under load; keep real headroom so a
# slow response is a wait rather than a missed check.
HTTP_TIMEOUT = 45.0


def env(name: str, required: bool = True) -> str:
    val = (os.environ.get(name) or "").strip()
    if required and not val:
        sys.exit(f"Missing required environment variable: {name}")
    return val


BUSINESS = env("BOOKINGS_BUSINESS")
SERVICE_ID = env("BOOKINGS_SERVICE_ID")
STAFF_ID = env("BOOKINGS_STAFF_ID")
SERVICE_MINUTES = int(env("SERVICE_MINUTES", False) or 20)
EARLIEST = env("EARLIEST_TIME", False) or "16:30"
BOOKING_URL = env("BOOKING_URL", False)
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHAT = env("TELEGRAM_CHAT_ID")

CURRENT_BOOKING = dt.datetime.fromisoformat(env("CURRENT_BOOKING"))
EARLIEST_H, EARLIEST_M = (int(x) for x in EARLIEST.split(":"))


def pretty(when: dt.datetime) -> str:
    hour = when.hour % 12 or 12
    ampm = "AM" if when.hour < 12 else "PM"
    return f"{when:%a %b} {when.day}, {hour}:{when.minute:02d} {ampm}"


def fetch_slots() -> list:
    """Ask for availability up to the held booking - nothing later can help."""
    now = dt.datetime.now()
    horizon = CURRENT_BOOKING + dt.timedelta(days=1)
    payload = {
        "staffIds": [STAFF_ID],
        "startDateTime": {
            "dateTime": now.strftime("%Y-%m-%dT00:00:00"),
            "timeZone": "Eastern Standard Time",
        },
        "endDateTime": {
            "dateTime": horizon.strftime("%Y-%m-%dT00:00:00"),
            "timeZone": "Eastern Standard Time",
        },
        "serviceId": SERVICE_ID,
    }
    url = f"{BASE}/{requests.utils.quote(BUSINESS)}/GetStaffAvailability"
    resp = requests.post(
        url,
        json=payload,
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": "greep-watch/1.0", "Accept": "application/json"},
    )
    resp.raise_for_status()
    blocks = resp.json().get("staffAvailabilityResponse") or []
    items = blocks[0].get("availabilityItems") or [] if blocks else []

    out = []
    for item in items:
        try:
            start = dt.datetime.fromisoformat(item["startDateTime"]["dateTime"][:19])
            end = dt.datetime.fromisoformat(item["endDateTime"]["dateTime"][:19])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({
            "start": start,
            "minutes": (end - start).total_seconds() / 60.0,
            "status": item.get("status", ""),
        })
    return out


def is_interesting(slot: dict, now: dt.datetime) -> bool:
    return (
        slot["status"].endswith("AVAILABLE")
        and slot["minutes"] >= SERVICE_MINUTES
        and slot["start"].time() >= dt.time(EARLIEST_H, EARLIEST_M)
        and now < slot["start"] < CURRENT_BOOKING
    )


def send_telegram(text: str) -> bool:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            return True
        print(f"telegram HTTP {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as exc:
        print(f"telegram failed: {exc}")
    return False


def check(alerted: set) -> tuple:
    """One pass. Returns (updated alerted set, number of live matches)."""
    now = dt.datetime.now()
    slots = fetch_slots()
    matches = sorted(
        (s for s in slots if is_interesting(s, now)), key=lambda s: s["start"]
    )
    live = {s["start"].strftime("%Y-%m-%dT%H:%M") for s in matches}

    fresh = [s for s in matches if s["start"].strftime("%Y-%m-%dT%H:%M") not in alerted]
    print(
        f"[{now:%H:%M:%S}] {len(slots)} windows, {len(matches)} match, {len(fresh)} new",
        flush=True,
    )

    newly = set()
    if fresh:
        lines = ["*Earlier Unit 8 DBA slot open*", ""]
        for s in fresh:
            earlier = (CURRENT_BOOKING - s["start"]).days
            lines.append(
                f"- *{pretty(s['start'])}* ({int(s['minutes'])} min) - {earlier}d earlier"
            )
        if BOOKING_URL:
            lines += ["", f"[Reschedule here]({BOOKING_URL})"]
        if send_telegram("\n".join(lines)):
            newly = {s["start"].strftime("%Y-%m-%dT%H:%M") for s in fresh}
            print("alert sent", flush=True)
        else:
            # Leave it unmarked so the next pass retries rather than losing it.
            print("alert FAILED - will retry next pass", flush=True)

    return (alerted & live) | newly, len(matches)


def main() -> int:
    """Single pass by default; --minutes turns it into a long-running loop.

    GitHub will not schedule a */5 cron reliably on a low-traffic repo, but a
    job that has already started runs to completion. So one triggered run
    loops internally for hours instead of exiting after 27 seconds.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=0, help="loop for this long")
    ap.add_argument("--interval", type=float, default=300, help="seconds between passes")
    args = ap.parse_args()

    try:
        alerted = set(json.loads(STATE_FILE.read_text()).get("alerted", []))
    except (OSError, json.JSONDecodeError):
        alerted = set()

    started = dt.datetime.now()
    deadline = started + dt.timedelta(minutes=args.minutes)
    passes = 0
    matches = 0

    while True:
        try:
            alerted, matches = check(alerted)
            passes += 1
        except Exception as exc:  # noqa: BLE001 - one bad request must not end the run
            print(f"pass failed: {exc}", flush=True)

        if args.minutes <= 0 or dt.datetime.now() >= deadline:
            break
        # Do not overshoot the deadline with a final long sleep.
        remaining = (deadline - dt.datetime.now()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(args.interval, remaining))

    STATE_FILE.write_text(
        json.dumps(
            {
                "alerted": sorted(alerted),
                "last_check": dt.datetime.now().isoformat(timespec="seconds"),
                "matches": matches,
                "passes": passes,
            },
            indent=2,
        )
    )
    print(f"done: {passes} passes over {(dt.datetime.now()-started)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
