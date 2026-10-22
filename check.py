#!/usr/bin/env python3
"""
Watch Mrs. Jewell's Microsoft Bookings calendar for an Algebra 2 DBA slot and
book it unattended, inside a hard monthly cap.

Runs on GitHub Actions so it does not need Arnav's laptop awake.

Differences from the Chemistry watcher this replaces (kept in
check_chem_backup.py):

  * It BOOKS. The Chem script was read-only by design and said so. This one
    submits a real appointment to a real teacher's calendar, so every guard
    below exists to stop it booking something wrong or booking too much.
  * No held booking to beat. There is nothing scheduled yet, so the target is
    simply "the earliest slot that satisfies every rule", not "earlier than X".
  * Service limits are read from the server each run rather than hardcoded.
    The teacher owns minimumLeadTime, maximumAdvance and the bookable windows;
    if she edits them, hardcoded copies would silently aim at slots that can
    no longer be booked.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE = "https://bookings.cloud.microsoft/BookingsService/api/V1/bookingBusinessesc2"
STATE_FILE = Path(__file__).resolve().parent / "state.json"

# Bump this whenever build_booking_payload() changes shape. Slots that failed
# permanently are remembered against the version that failed, so fixing the
# payload automatically makes them eligible again instead of needing the
# failure list cleared by hand.
PAYLOAD_VERSION = 2
HTTP_TIMEOUT = 45.0
TZ_NAME = "Eastern Standard Time"
UA = "greep-watch/2.0 (personal appointment watcher)"


def env(name: str, required: bool = True, default: str = "") -> str:
    val = (os.environ.get(name) or "").strip()
    if not val and required:
        sys.exit(f"Missing required environment variable: {name}")
    return val or default


BUSINESS = env("BOOKINGS_BUSINESS", False, "MrsJewell@live.pasco.k12.fl.us")
SERVICE_ID = env("BOOKINGS_SERVICE_ID", False,
                 "b6b103ca-a507-4356-b844-d19fb5f58482")
STAFF_ID = env("BOOKINGS_STAFF_ID", False,
               "5f694a81-d2f1-4851-9647-07d01b109c98")

# Arnav's own words: book after 4:20 PM. This matters more than it looks -
# see the note on bookable windows in eligible().
EARLIEST = env("EARLIEST_TIME", False, "16:20")
EARLIEST_H, EARLIEST_M = (int(x) for x in EARLIEST.split(":"))

# Identity submitted with the booking. No defaults on purpose: a booking that
# went out under a placeholder name would be worse than no booking.
CUSTOMER_NAME = env("CUSTOMER_NAME", False)
CUSTOMER_PHONE = env("CUSTOMER_PHONE", False)
CUSTOMER_EMAIL = env("CUSTOMER_EMAIL", False)
COURSE_ENROLLMENT = env("COURSE_ENROLLMENT", False, "Alg 2 / Alg 2 Honors")

# Hard ceilings. MONTHLY_CAP is the "only 2 a month" rule; CONCURRENT_CAP is
# the "only 2 at once" half of the same sentence.
MONTHLY_CAP = int(env("MONTHLY_CAP", False, "2"))
CONCURRENT_CAP = int(env("CONCURRENT_CAP", False, "2"))

TG_TOKEN = env("TELEGRAM_BOT_TOKEN", False)
TG_CHAT = env("TELEGRAM_CHAT_ID", False)
BOOKING_URL = env("BOOKING_URL", False,
                  f"https://bookings.cloud.microsoft/book/{BUSINESS}/")


def redact(text: str) -> str:
    """Mask Arnav's name, phone and email before anything is printed.

    This repo is public, so GitHub Actions logs are world-readable. The
    booking payload and the server's echo of it both carry his phone number
    and email, which would otherwise be permanently searchable in the log of
    every run that booked anything.
    """
    out = text
    for secret in (CUSTOMER_PHONE, CUSTOMER_EMAIL, CUSTOMER_NAME):
        if not secret or len(secret) <= 3:
            continue
        out = out.replace(secret, "[redacted]")
        # Phone numbers come back reformatted - "555-010-1234", "+15550101234",
        # "(555) 010-1234" - so match the digit run with any separators
        # between digits rather than the one spelling we were given.
        digits = re.sub(r"\D", "", secret)
        if len(digits) >= 7:
            loose = r"\+?1?\D{0,2}" + r"\D{0,2}".join(digits)
            out = re.sub(loose, "[redacted]", out)
    return out


def pretty(when: dt.datetime) -> str:
    """'Tue Nov 4, 6:00 PM' - built by hand; %-I and %-d are glibc-only."""
    hour = when.hour % 12 or 12
    ampm = "AM" if when.hour < 12 else "PM"
    return f"{when:%a %b} {when.day}, {hour}:{when.minute:02d} {ampm}"


def iso_duration_days(text: str, fallback: int) -> int:
    """'P45D' -> 45. Only whole days are used by this calendar."""
    m = re.fullmatch(r"P(\d+)D", (text or "").strip())
    return int(m.group(1)) if m else fallback


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    return s


# --------------------------------------------------------------------------
# Server-side service limits
# --------------------------------------------------------------------------

def fetch_policy(sess: requests.Session) -> dict:
    """Read this service's real limits instead of trusting local constants."""
    url = f"{BASE}/{requests.utils.quote(BUSINESS)}/services"
    resp = sess.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    arr = body if isinstance(body, list) else next(
        (v for v in body.values() if isinstance(v, list)), []
    )
    svc = next((s for s in arr if s.get("serviceId") == SERVICE_ID), None)
    if svc is None:
        raise RuntimeError(f"service {SERVICE_ID} is gone from this calendar")

    pol = svc.get("bookingsSchedulingPolicy") or {}
    minutes = int(re.sub(r"\D", "", svc.get("defaultDuration") or "PT30M") or 30)
    required_qids = [
        q["questionGuid"] for q in (svc.get("customQuestions") or [])
        if q.get("required")
    ]
    return {
        "title": svc.get("title") or "",
        "minutes": minutes,
        "lead_days": iso_duration_days(pol.get("minimumLeadTime"), 1),
        "advance_days": iso_duration_days(pol.get("maximumAdvance"), 45),
        "required_qids": required_qids,
    }


def fetch_slots(sess: requests.Session, policy: dict) -> list:
    now = dt.datetime.now()
    horizon = now + dt.timedelta(days=policy["advance_days"] + 1)
    payload = {
        "staffIds": [STAFF_ID],
        "startDateTime": {"dateTime": now.strftime("%Y-%m-%dT00:00:00"),
                          "timeZone": TZ_NAME},
        "endDateTime": {"dateTime": horizon.strftime("%Y-%m-%dT00:00:00"),
                        "timeZone": TZ_NAME},
        "serviceId": SERVICE_ID,
    }
    url = f"{BASE}/{requests.utils.quote(BUSINESS)}/GetStaffAvailability"
    resp = sess.post(url, json=payload, timeout=HTTP_TIMEOUT)
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
            "end": end,
            "minutes": (end - start).total_seconds() / 60.0,
            "status": item.get("status", ""),
        })
    return out


def eligible(slot: dict, now: dt.datetime, policy: dict) -> bool:
    """Is this slot one Arnav could actually attend and actually book?

    The wall-clock rule is the binding one. This service's bookable windows
    are Mon/Wed 10:00-13:30, Tue/Thu 11:00-13:00 and 18:00-20:00, Fri
    10:00-12:00 - so an "after 4:20 PM" filter leaves only the Tue/Thu
    evening block. That is intended (everything else is during school) but it
    means roughly five candidate start times, twice a week.
    """
    if not slot["status"].endswith("AVAILABLE"):
        return False
    if slot["minutes"] < policy["minutes"]:
        return False
    if slot["start"].time() < dt.time(EARLIEST_H, EARLIEST_M):
        return False
    # The server rejects anything inside minimumLeadTime, so never aim there.
    if slot["start"] < now + dt.timedelta(days=policy["lead_days"]):
        return False
    if slot["start"] > now + dt.timedelta(days=policy["advance_days"]):
        return False
    return True


def key_of(slot: dict) -> str:
    return slot["start"].strftime("%Y-%m-%dT%H:%M")


# --------------------------------------------------------------------------
# State: the cap lives here, so it has to survive a crashed run
# --------------------------------------------------------------------------

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("alerted", [])
    data.setdefault("booked", [])      # [{slot, booked_at, confirmed, id}]
    data.setdefault("failed", {})      # slot_key -> {version, error, at}
    return data


def save_state(data: dict) -> None:
    data["last_check"] = dt.datetime.now().isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")


def booked_in_month(data: dict, month: str) -> list:
    """Bookings whose APPOINTMENT falls in `month` ('2026-11').

    Counted by appointment date, not booking date: "2 DBAs in the month" is
    about how many sessions he sits in a month, so a slot booked Oct 30 for
    Nov 4 belongs to November.
    """
    return [b for b in data["booked"] if b["slot"][:7] == month]


def booked_upcoming(data: dict, now: dt.datetime) -> list:
    """Bookings still in the future - the "at once" count."""
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    return [b for b in data["booked"] if b["slot"] > stamp]


def cap_blocks(data: dict, now: dt.datetime, slot: dict) -> str:
    """Return a reason this slot must not be booked, or '' if it is allowed."""
    month = key_of(slot)[:7]
    same_month = booked_in_month(data, month)
    if len(same_month) >= MONTHLY_CAP:
        return f"{len(same_month)} already booked in {month} (cap {MONTHLY_CAP})"
    upcoming = booked_upcoming(data, now)
    if len(upcoming) >= CONCURRENT_CAP:
        return f"{len(upcoming)} upcoming DBAs already held (cap {CONCURRENT_CAP})"
    if any(b["slot"] == key_of(slot) for b in data["booked"]):
        return "this exact slot is already recorded as booked"
    # A 400 means the request itself is wrong, so the next identical attempt
    # fails identically. The first version of this script retried one bad
    # payload 18 times in two hours, churning a reserve/release commit pair
    # each time and hammering Microsoft from a school account. Remember the
    # refusal against PAYLOAD_VERSION and stop until the payload changes.
    prior = data["failed"].get(key_of(slot))
    if prior and prior.get("version") == PAYLOAD_VERSION:
        return f"payload v{PAYLOAD_VERSION} was already refused: {prior.get('error', '')[:80]}"
    return ""


def git_persist(message: str) -> bool:
    """Commit and push state.json, reporting honestly whether it landed.

    The old workflow did `git push || true`, which is fine for a dedup set -
    losing it only costs a duplicate alert. It is NOT fine for a cap: if the
    push is swallowed, the next run starts from stale state, sees zero
    bookings, and books again. So this returns a real boolean and the caller
    reserves BEFORE it books.
    """
    here = Path(__file__).resolve().parent
    setup = [
        ["git", "config", "user.name", "greep-watch"],
        ["git", "config", "user.email", "actions@users.noreply.github.com"],
        ["git", "add", "state.json"],
    ]
    try:
        for cmd in setup:
            subprocess.run(cmd, cwd=here, check=True, capture_output=True)
        status = subprocess.run(["git", "status", "--porcelain", "state.json"],
                                cwd=here, capture_output=True, text=True)
        if not status.stdout.strip():
            return True  # nothing to persist is a success, not a failure
        for cmd in (["git", "commit", "-m", message],
                    ["git", "pull", "--rebase", "--autostash", "origin", "main"],
                    ["git", "push"]):
            subprocess.run(cmd, cwd=here, check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        print(f"state push FAILED: {exc} {detail[:200]!r}", flush=True)
        return False


# --------------------------------------------------------------------------
# Booking
# --------------------------------------------------------------------------

def build_booking_payload(slot: dict, policy: dict) -> dict:
    end = slot["start"] + dt.timedelta(minutes=policy["minutes"])
    answers = [{"questionId": qid, "answer": COURSE_ENROLLMENT}
               for qid in policy["required_qids"]]
    return {
        "serviceId": SERVICE_ID,
        "serviceName": policy["title"],
        "staffMemberIds": [STAFF_ID],
        "startDateTime": {
            "dateTime": slot["start"].strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": TZ_NAME,
        },
        "endDateTime": {
            "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": TZ_NAME,
        },
        "customers": [{
            "name": CUSTOMER_NAME,
            "emailAddress": CUSTOMER_EMAIL,
            "phone": CUSTOMER_PHONE,
            "timeZone": TZ_NAME,
            "customQuestionAnswers": answers,
        }],
        "customerTimeZone": TZ_NAME,
        "isLocationOnline": True,
    }


def book(sess: requests.Session, slot: dict, policy: dict, dry: bool) -> tuple:
    """Submit the booking. Returns (ok, detail, permanent).

    `permanent` says whether retrying this exact payload is pointless. A 4xx
    is the server rejecting the request as malformed or the slot as gone -
    identical retries get identical refusals. A 5xx, a timeout or a 429 is
    worth another pass.

    The exact field names of this endpoint are not published; the route is
    confirmed live and anonymous (an empty POST returns 500, not 401 or 404),
    and the full response body is logged either way so a rejection is
    diagnosable rather than mysterious.
    """
    payload = build_booking_payload(slot, policy)
    if dry:
        print("DRY RUN - would POST:", flush=True)
        print(redact(json.dumps(payload, indent=2)), flush=True)
        return False, "dry run", False

    missing = [n for n, v in (("CUSTOMER_NAME", CUSTOMER_NAME),
                              ("CUSTOMER_PHONE", CUSTOMER_PHONE)) if not v]
    if missing:
        return False, f"refusing to book without {', '.join(missing)}", True

    url = f"{BASE}/{requests.utils.quote(BUSINESS)}/appointments"
    try:
        resp = sess.post(url, json=payload, timeout=HTTP_TIMEOUT,
                         headers={"Content-Type": "application/json"})
    except requests.RequestException as exc:
        return False, f"network error: {exc}", False

    body = redact((resp.text or "")[:800])
    print(f"booking HTTP {resp.status_code}: {body}", flush=True)
    if resp.status_code in (200, 201):
        try:
            parsed = resp.json()
            appt_id = parsed.get("appointmentId") or parsed.get("id") or ""
        except ValueError:
            appt_id = ""
        return True, appt_id, False

    # 429 is "slow down", not "never"; everything else in 4xx is a real refusal.
    permanent = 400 <= resp.status_code < 500 and resp.status_code != 429
    return False, f"HTTP {resp.status_code}: {body}", permanent


# --------------------------------------------------------------------------
# Notification
# --------------------------------------------------------------------------

def send_telegram(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        print("telegram not configured", flush=True)
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            return True
        print(f"telegram HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
    except requests.RequestException as exc:
        print(f"telegram failed: {exc}", flush=True)
    return False


# --------------------------------------------------------------------------
# One pass
# --------------------------------------------------------------------------

def check(sess: requests.Session, data: dict, policy: dict, dry: bool) -> dict:
    now = dt.datetime.now()
    slots = fetch_slots(sess, policy)
    matches = sorted((s for s in slots if eligible(s, now, policy)),
                     key=lambda s: s["start"])
    print(f"[{now:%m-%d %H:%M:%S}] {len(slots)} windows, {len(matches)} eligible",
          flush=True)

    if not matches:
        return data

    target = matches[0]
    reason = cap_blocks(data, now, target)
    if reason:
        # Still worth one alert: he may want to cancel something to take it.
        if key_of(target) not in data["alerted"]:
            if send_telegram(
                f"*Algebra 2 DBA slot open (not booked)*\n\n"
                f"- *{pretty(target['start'])}* ({int(target['minutes'])} min)\n\n"
                f"Held back: {reason}.\n\n[Open calendar]({BOOKING_URL})"
            ):
                data["alerted"] = sorted(set(data["alerted"]) | {key_of(target)})
                save_state(data)
        print(f"not booking {key_of(target)}: {reason}", flush=True)
        return data

    # Reserve first, book second. If the reservation cannot be persisted, the
    # cap is not enforceable on the next run, so decline rather than risk a
    # third booking. A lost slot is recoverable; an over-booking is not.
    record = {"slot": key_of(target),
              "booked_at": now.isoformat(timespec="seconds"),
              "confirmed": False, "id": ""}
    data["booked"] = sorted(data["booked"] + [record], key=lambda b: b["slot"])
    save_state(data)
    if not dry and not git_persist(f"reserve {key_of(target)} [skip ci]"):
        data["booked"] = [b for b in data["booked"] if b["slot"] != key_of(target)]
        save_state(data)
        print("could not persist reservation - declining to book", flush=True)
        return data

    ok, detail, permanent = book(sess, target, policy, dry)
    if ok:
        for b in data["booked"]:
            if b["slot"] == key_of(target):
                b["confirmed"], b["id"] = True, detail
        save_state(data)
        git_persist(f"booked {key_of(target)} [skip ci]")
        send_telegram(
            f"*Algebra 2 DBA BOOKED*\n\n"
            f"- *{pretty(target['start'])}* ({policy['minutes']} min)\n"
            f"- Course: {COURSE_ENROLLMENT}\n\n"
            f"[Manage booking]({BOOKING_URL})"
        )
        print(f"BOOKED {key_of(target)}", flush=True)
    else:
        # Release the reservation so a real slot is not permanently burned by
        # a failed attempt, and so a dry run leaves no trace in state.
        data["booked"] = [b for b in data["booked"] if b["slot"] != key_of(target)]
        if permanent:
            # Record it so cap_blocks() stops retrying this payload, and say
            # so once rather than on every pass for the rest of the run.
            data["failed"][key_of(target)] = {
                "version": PAYLOAD_VERSION,
                "error": detail[:200],
                "at": now.isoformat(timespec="seconds"),
            }
        save_state(data)
        if not dry:
            git_persist(f"release {key_of(target)} [skip ci]")
            send_telegram(
                f"*Algebra 2 DBA booking FAILED*\n\n"
                f"- *{pretty(target['start'])}*\n- {detail[:300]}\n\n"
                + ("No further attempts on this slot until the payload "
                   "changes.\n\n" if permanent else "")
                + f"[Book manually]({BOOKING_URL})"
            )
        print(f"booking not completed ({'permanent' if permanent else 'transient'})"
              f": {detail}", flush=True)
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Watch and book an Algebra 2 DBA.")
    ap.add_argument("--minutes", type=float, default=0,
                    help="loop for this many minutes (0 = single pass)")
    ap.add_argument("--interval", type=float, default=300,
                    help="seconds between passes")
    ap.add_argument("--book", action="store_true",
                    help="actually submit bookings (default is a dry run)")
    ap.add_argument("--status", action="store_true",
                    help="list open windows and the cap state, then exit")
    args = ap.parse_args()

    sess = session()
    policy = fetch_policy(sess)
    data = load_state()
    now = dt.datetime.now()

    print(f"service: {policy['title']} ({policy['minutes']} min), "
          f"lead {policy['lead_days']}d, advance {policy['advance_days']}d",
          flush=True)
    print(f"rules: start >= {EARLIEST}, cap {MONTHLY_CAP}/month, "
          f"{CONCURRENT_CAP} at once, "
          f"mode={'BOOK' if args.book else 'dry-run'}", flush=True)

    if args.status:
        slots = fetch_slots(sess, policy)
        free = sorted((s for s in slots if s["status"].endswith("AVAILABLE")),
                      key=lambda s: s["start"])
        print(f"{len(free)} open window(s):")
        for s in free:
            mark = "ELIGIBLE" if eligible(s, now, policy) else "        "
            print(f"  {mark} {pretty(s['start']):<26} {int(s['minutes']):>3} min")
        print(f"booked in {now:%Y-%m}: "
              f"{len(booked_in_month(data, f'{now:%Y-%m}'))}/{MONTHLY_CAP}")
        print(f"upcoming held:    "
              f"{len(booked_upcoming(data, now))}/{CONCURRENT_CAP}")
        return 0

    started = dt.datetime.now()
    deadline = started + dt.timedelta(minutes=args.minutes)
    passes = 0
    while True:
        try:
            data = check(sess, data, policy, dry=not args.book)
            passes += 1
        except Exception as exc:  # noqa: BLE001 - one bad pass must not end the run
            print(f"pass failed: {exc}", flush=True)

        if args.minutes <= 0:
            break
        remaining = (deadline - dt.datetime.now()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(args.interval, remaining))

    data["passes"] = passes
    save_state(data)
    print(f"done: {passes} passes over {dt.datetime.now() - started}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
