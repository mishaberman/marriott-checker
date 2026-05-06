#!/usr/bin/env python3
"""
Marriott Bonvoy points availability watcher.

Default target:
  Residence Inn by Marriott Wenatchee (Marriott hotel code: EATRI)
  Check-in: 2026-06-19
  Check-out: 2026-06-20
  1 room, 1 adult, use rewards points

What it does:
  - Opens Marriott in a real browser with Playwright.
  - Looks for points pricing / bookable award inventory.
  - Sends an email only when availability flips from unavailable -> available.
  - Stores state locally so you do not get spammed every run.

Install:
  python3 -m pip install -r requirements.txt
  python3 -m playwright install chromium

Run once:
  python3 marriott_points_watch.py --debug

Run a test email:
  python3 marriott_points_watch.py --test-email
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


# Direct rate-list URL for Residence Inn Wenatchee, using points, one night.
# You can override this with WATCH_URL in .env if Marriott changes its URL format.
DEFAULT_WATCH_URL = (
    "https://www.marriott.com/reservation/rateListMenu.mi"
    "?propertyCode=EATRI"
    "&fromDate=06/19/2026"
    "&toDate=06/20/2026"
    "&numberOfRooms=1"
    "&numberOfAdults=1"
    "&useRewardsPoints=true"
)

DEFAULT_BOOKING_URL = (
    "https://www.marriott.com/en-us/hotels/eatri-residence-inn-wenatchee/overview/"
)

DEFAULT_HOTEL_NAME = "Residence Inn by Marriott Wenatchee"
STATE_FILE = Path(os.getenv("STATE_FILE", ".marriott_points_watch_state.json"))
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "marriott_watch_debug"))


@dataclass
class WatchResult:
    available: bool
    reason: str
    points_matches: list[str]
    url: str
    checked_at: str
    title: str | None = None


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader so this script does not require python-dotenv."""
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def analyze_page_text(text: str, current_url: str, title: str | None = None) -> WatchResult:
    """
    Heuristic detection.

    Marriott pages are dynamic and selectors change, so we rely mostly on visible text:
      Strong positive: actual points price like "45,000 Points" / "45,000 points/night".
      Strong negative: visible no-award/no-rate/sold-out language.
    """
    compact = normalize_text(text)
    lower = compact.lower()

    # Common Marriott / hotel booking negative states.
    negative_phrases = [
        "not available for check-in",
        "not available",
        "unavailable",
        "sold out",
        "no rooms available",
        "no rates available",
        "rooms are not available",
        "points redemption is not available",
        "there are no rooms available",
        "we couldn’t find any available rooms",
        "we couldn't find any available rooms",
        "change your dates",
        "try different dates",
    ]

    # Marriott usually renders award pricing as e.g. "43,000 Points" or "43,000 points/night".
    points_matches = sorted(
        set(re.findall(r"\b(?:[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d{3,6})\s*(?:points|pts)\b", compact, flags=re.I))
    )

    has_negative = any(phrase in lower for phrase in negative_phrases)

    # Strongest signal: points price exists and the page is not clearly saying no rooms/rates.
    if points_matches and not has_negative:
        return WatchResult(
            available=True,
            reason=f"Found points pricing: {', '.join(points_matches[:5])}",
            points_matches=points_matches,
            url=current_url,
            checked_at=utc_now(),
            title=title,
        )

    # Secondary signal for pages that show a select/book button next to points text.
    points_language = any(token in lower for token in ["points", "bonvoy points", "use points"])
    booking_language = any(token in lower for token in ["select", "book now", "view rates", "continue"])
    if points_language and booking_language and not has_negative:
        return WatchResult(
            available=True,
            reason="Found points-related booking language without an obvious unavailable message.",
            points_matches=points_matches,
            url=current_url,
            checked_at=utc_now(),
            title=title,
        )

    if has_negative:
        return WatchResult(
            available=False,
            reason="Found unavailable / sold-out / no-rates language.",
            points_matches=points_matches,
            url=current_url,
            checked_at=utc_now(),
            title=title,
        )

    return WatchResult(
        available=False,
        reason="No clear points price or award-booking signal found.",
        points_matches=points_matches,
        url=current_url,
        checked_at=utc_now(),
        title=title,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(result: WatchResult) -> None:
    payload = {
        "last_checked_at": result.checked_at,
        "last_available": result.available,
        "last_reason": result.reason,
        "last_url": result.url,
        "last_title": result.title,
        "last_points_matches": result.points_matches,
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def send_email(subject: str, body: str) -> None:
    smtp_host = require_env("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = require_env("SMTP_USER")
    smtp_pass = require_env("SMTP_PASS")
    email_from = os.getenv("EMAIL_FROM", smtp_user)
    email_to = require_env("EMAIL_TO")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(body)

    if smtp_port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def check_marriott(url: str, headed: bool = False, debug: bool = False, timeout_ms: int = 60_000) -> WatchResult:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            # Cookie banners / modals are inconsistent; try to close anything obvious.
            for label in ["Accept All", "Accept", "I agree", "Close", "No Thanks"]:
                try:
                    page.get_by_role("button", name=re.compile(label, re.I)).click(timeout=2_000)
                    break
                except Exception:
                    pass

            # Let client-rendered rates finish loading.
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(5_000)

            title = page.title()
            body_text = page.locator("body").inner_text(timeout=15_000)
            current_url = page.url

            if debug:
                DEBUG_DIR.mkdir(exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                (DEBUG_DIR / f"marriott_{stamp}.txt").write_text(body_text, encoding="utf-8")
                page.screenshot(path=str(DEBUG_DIR / f"marriott_{stamp}.png"), full_page=True)

            return analyze_page_text(body_text, current_url, title=title)
        finally:
            context.close()
            browser.close()


def build_alert_body(hotel_name: str, result: WatchResult, booking_url: str) -> str:
    points = ", ".join(result.points_matches) if result.points_matches else "Possible points availability detected"
    return f"""Good news — {hotel_name} may now be bookable with Marriott Bonvoy points.

Detected: {points}
Reason: {result.reason}
Checked at: {result.checked_at}

Open Marriott:
{booking_url}

Checked URL:
{result.url}

Note: Marriott availability can change quickly. Open the link and book immediately if it is still available.
"""


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Watch Marriott points availability and email when available.")
    parser.add_argument("--url", default=os.getenv("WATCH_URL", DEFAULT_WATCH_URL), help="Marriott URL to check")
    parser.add_argument("--booking-url", default=os.getenv("BOOKING_URL", DEFAULT_BOOKING_URL), help="URL to include in alert email")
    parser.add_argument("--hotel-name", default=os.getenv("HOTEL_NAME", DEFAULT_HOTEL_NAME), help="Hotel name for email subject")
    parser.add_argument("--debug", action="store_true", help="Save page text and screenshot to debug folder")
    parser.add_argument("--headed", action="store_true", help="Run browser visibly for troubleshooting")
    parser.add_argument("--test-email", action="store_true", help="Send a test email and exit")
    parser.add_argument("--alert-every-available", action="store_true", help="Email on every run where available is detected")
    args = parser.parse_args()

    if args.test_email:
        send_email(
            subject="Marriott points watcher test email",
            body="If you received this, SMTP email is configured correctly.",
        )
        print("Test email sent.")
        return 0

    result = check_marriott(args.url, headed=args.headed, debug=args.debug)
    previous = read_state()
    previous_available = bool(previous.get("last_available"))

    print(json.dumps(result.__dict__, indent=2))

    should_alert = result.available and (args.alert_every_available or not previous_available)
    if should_alert:
        subject = f"Marriott points available? {args.hotel_name}"
        body = build_alert_body(args.hotel_name, result, args.booking_url)
        send_email(subject, body)
        print("Alert email sent.")
    elif result.available:
        print("Available, but alert already sent previously. Use --alert-every-available to email every time.")
    else:
        print("Not available yet.")

    write_state(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
