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
PAYLOAD_VERSION = 4
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

# Latest acceptable START. The service publishes 18:00-20:00 Tue/Thu windows
# that the booking page never offers, so without a ceiling a midday floor
# would aim the watcher at evening starts no customer can book.
LATEST = env("LATEST_TIME", False, "23:59")
LATEST_H, LATEST_M = (int(x) for x in LATEST.split(":"))

# Midnight is when a new day lands on the 45-day advance window, so poll fast
# across it and slowly the rest of the time.
BURST_INTERVAL = float(env("BURST_INTERVAL", False, "15"))
BURST_START, BURST_END = dt.time(23, 58), dt.time(0, 6)

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


LOCK_FILE = Path(__file__).resolve().parent / "check.lock"
_LOCK_HANDLE = None   # held for the process lifetime; the OS frees it on exit


def acquire_lock() -> bool:
    """Take an exclusive OS lock so two watchers cannot run at once.

    One pass costs 0.03 CPU-seconds over ~12 seconds of wall clock - it is
    waiting on the network, not computing - so a single watcher is
    unmeasurable. Stacked instances are the only realistic way this becomes a
    CPU or rate-limit problem, and the way that happens is a forgotten window
    plus a new run. An OS lock cannot be sidestepped by deleting a file, and
    the kernel releases it when the holder dies, so a crash leaves nothing
    stale behind.
    """
    global _LOCK_HANDLE
    try:
        fh = LOCK_FILE.open("a+")
    except OSError:
        return True  # cannot create a lock file; do not block the watcher
    try:
        try:
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _LOCK_HANDLE = fh
    return True


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

    # The real payload embeds each question's whole definition, not just its
    # id, so fetch the definitions and key them by guid.
    qdefs = {}
    try:
        qresp = sess.get(f"{BASE}/{requests.utils.quote(BUSINESS)}/customQuestions",
                         timeout=HTTP_TIMEOUT)
        qresp.raise_for_status()
        qbody = qresp.json()
        qarr = qbody if isinstance(qbody, list) else next(
            (v for v in qbody.values() if isinstance(v, list)), [])
        qdefs = {q["id"]: q for q in qarr if q.get("id")}
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"WARN: could not load custom questions: {exc}", flush=True)

    questions = []
    for q in (svc.get("customQuestions") or []):
        guid = q.get("questionGuid")
        definition = qdefs.get(guid)
        if definition:
            questions.append({"definition": definition,
                              "required": bool(q.get("required"))})

    # The service's own bookable windows, which are narrower than the
    # business hours and are what the booking page actually offers.
    windows = {}
    for block in (pol.get("bookingServiceWindows") or []):
        for day in (block.get("bookableHours") or []):
            windows.setdefault(day.get("day", ""), []).extend(
                (s.get("start", 0), s.get("end", 0))
                for s in (day.get("timeSlots") or [])
            )

    # Start times sit on this grid, anchored to each window's start - the
    # booking page offers 11:00/11:20/11:40/12:00/12:20 for a 20-minute
    # interval inside an 11:00-13:00 window.
    interval = int(re.sub(r"\D", "", pol.get("timeSlotInterval") or "PT20M") or 20)

    return {
        "title": svc.get("title") or "",
        "minutes": minutes,
        "interval": max(5, interval),
        "lead_days": iso_duration_days(pol.get("minimumLeadTime"), 1),
        "advance_days": iso_duration_days(pol.get("maximumAdvance"), 45),
        "questions": questions,
        "windows": windows,
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


def within_service_window(start: dt.datetime, policy: dict) -> bool:
    """Does a full appointment starting here fit inside a bookable window?

    bookableHours are minutes from midnight, per weekday name. The whole
    appointment must fit, not just its start, or the server rejects it.
    """
    windows = policy.get("windows") or {}
    if not windows:
        return True  # nothing published; do not invent a restriction
    slots = windows.get(start.strftime("%A")) or []
    begin = start.hour * 60 + start.minute
    finish = begin + policy["minutes"]
    return any(w_start <= begin and finish <= w_end for w_start, w_end in slots)


def eligible(slot: dict, now: dt.datetime, policy: dict) -> bool:
    """Is this slot one Arnav could actually attend and actually book?

    Two separate calendars are at work and conflating them produced phantom
    slots. GetStaffAvailability returns when the TEACHER is free; the service
    carries its own narrower bookableHours, and the booking page only offers
    the intersection. Filtering on staff availability alone reported a Dec 1
    6:30 PM opening that the page does not list at all, so the watcher would
    have kept trying to book a time no customer can book.
    """
    if not slot["status"].endswith("AVAILABLE"):
        return False
    if slot["minutes"] < policy["minutes"]:
        return False
    if slot["start"].time() < dt.time(EARLIEST_H, EARLIEST_M):
        return False
    if slot["start"].time() > dt.time(LATEST_H, LATEST_M):
        return False
    if not within_service_window(slot["start"], policy):
        return False
    # The server rejects anything inside minimumLeadTime, so never aim there.
    if slot["start"] < now + dt.timedelta(days=policy["lead_days"]):
        return False
    if slot["start"] > now + dt.timedelta(days=policy["advance_days"]):
        return False
    return True


def candidates(gaps: list, policy: dict) -> list:
    """Expand free gaps into the individual start times a customer can pick.

    A free gap is not a slot. An 11:00-13:00 gap on a 20-minute grid offers
    five bookable starts, and judging the gap by its 11:00 opening alone threw
    away 12:00 and 12:20 - the only two Arnav can attend, since he is in class
    until noon. Starts are anchored to each service window's opening, which
    is how the booking page produces 11:00/11:20/11:40/12:00/12:20.
    """
    step = dt.timedelta(minutes=policy["interval"])
    length = dt.timedelta(minutes=policy["minutes"])
    windows = policy.get("windows") or {}
    seen, out = set(), []
    for gap in gaps:
        if not gap["status"].endswith("AVAILABLE"):
            continue
        day = gap["start"].replace(hour=0, minute=0, second=0, microsecond=0)
        for w_start, w_end in windows.get(gap["start"].strftime("%A")) or []:
            start = day + dt.timedelta(minutes=w_start)
            limit = min(day + dt.timedelta(minutes=w_end), gap["end"])
            while start + length <= limit:
                if start >= gap["start"] and start not in seen:
                    seen.add(start)
                    out.append({"start": start, "end": start + length,
                                "minutes": float(policy["minutes"]),
                                "status": "AVAILABLE"})
                start += step
    return sorted(out, key=lambda s: s["start"])


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
    """Build the create-appointment body.

    Field names were established by probing the endpoint with single-field
    payloads; it answers 400 "Unknown field: X" for anything it does not
    recognise, so a probe that stops complaining about X confirms X.

    What that established:
      * the body is wrapped in "appointment" - the only wrapper accepted
      * the time fields are startTime/endTime, NOT startDateTime/endDateTime,
        and each is a {dateTime, timeZone} object
      * it is a protobuf service, so "duration" is a google.protobuf.Duration
        and serialises as "1800s", not ISO-8601 "PT30M"
      * customers[] takes name, emailAddress, phone, timeZone and notes;
        displayName, email, address and customerId are all rejected

    The guessing was eventually abandoned in favour of observing the real
    client: an interceptor in the browser captured the body the booking page
    itself sends, with the request blocked so nothing was created. This mirrors
    that capture. Three fields the probes had "confirmed" were wrong in it -
    there is no `duration` and no `serviceName`, and `isLocationOnline` is
    false, not true - which is why the captured body wins over inference.

    The custom-question answer lives in customers[].answeredCustomQuestions
    and embeds the question's whole definition, not just its id. That is why
    no amount of guessing a flat {questionId, answer} shape ever landed.
    """
    end = slot["start"] + dt.timedelta(minutes=policy["minutes"])

    answered = []
    for q in policy["questions"]:
        definition = q["definition"]
        options = definition.get("answerOptions") or []
        # selectedOptions is the chosen option's INDEX as a string.
        selected = [str(options.index(COURSE_ENROLLMENT))] \
            if COURSE_ENROLLMENT in options else []
        answered.append({
            "customQuestion": {
                "id": definition.get("id", ""),
                "questionText": definition.get("questionText", ""),
                "answerOptions": options,
                "answerInputType": definition.get("answerInputType", ""),
            },
            "answer": COURSE_ENROLLMENT if selected else "",
            "isRequired": q["required"],
            "selectedOptions": selected,
        })

    return {
        "appointment": {
            "startTime": {
                "dateTime": slot["start"].strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": TZ_NAME,
            },
            "endTime": {
                "dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": TZ_NAME,
            },
            "serviceId": SERVICE_ID,
            "staffMemberIds": [STAFF_ID],
            "customers": [{
                "name": CUSTOMER_NAME,
                "emailAddress": CUSTOMER_EMAIL,
                "phone": CUSTOMER_PHONE,
                "notes": "",
                "timeZone": TZ_NAME,
                "answeredCustomQuestions": answered,
                "location": {"displayName": "",
                             "address": {"street": "", "type": "Other"}},
                "smsNotificationsEnabled": False,
                "instanceId": "",
                "price": 0,
                "priceType": "SERVICEDEFAULTPRICETYPES_NOT_SET",
            }],
            "isLocationOnline": False,
            "smsNotificationsEnabled": False,
            "verificationCode": "",
            "customerTimeZone": TZ_NAME,
            "trackingDataId": "",
            "bookingFormInfoList": [],
            "price": 0,
            "priceType": "SERVICEDEFAULTPRICETYPES_NOT_SET",
            "isAllDay": False,
            "additionalRecipients": [],
        }
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
    matches = [s for s in candidates(slots, policy) if eligible(s, now, policy)]
    print(f"[{now:%m-%d %H:%M:%S}] {len(slots)} gaps, {len(matches)} eligible starts",
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
    ap.add_argument("--force", action="store_true",
                    help="start even if another watcher holds the lock")
    args = ap.parse_args()

    # --status is one read and exits, so it never needs to hold the lock.
    if not args.status and not acquire_lock() and not args.force:
        print("another watcher already holds the lock; refusing to start a "
              "second. Stop it, or pass --force.", flush=True)
        return 1

    sess = session()
    policy = fetch_policy(sess)
    data = load_state()
    now = dt.datetime.now()

    print(f"service: {policy['title']} ({policy['minutes']} min), "
          f"lead {policy['lead_days']}d, advance {policy['advance_days']}d",
          flush=True)
    print(f"rules: {EARLIEST} <= start <= {LATEST}, cap {MONTHLY_CAP}/month, "
          f"{CONCURRENT_CAP} at once, "
          f"mode={'BOOK' if args.book else 'dry-run'}", flush=True)

    if args.status:
        slots = fetch_slots(sess, policy)
        starts = candidates(slots, policy)
        good = [s for s in starts if eligible(s, now, policy)]
        print(f"{len(starts)} bookable start(s), {len(good)} eligible:")
        for s in good:
            print(f"  ELIGIBLE {pretty(s['start']):<26} {int(s['minutes']):>3} min")
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
        t = dt.datetime.now().time()
        in_burst = t >= BURST_START or t <= BURST_END  # wraps past midnight
        # Still I/O-bound: a 15s burst is ~25 short requests, each spent
        # almost entirely waiting on the network.
        time.sleep(min(BURST_INTERVAL if in_burst else args.interval, remaining))

    data["passes"] = passes
    save_state(data)
    print(f"done: {passes} passes over {dt.datetime.now() - started}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
