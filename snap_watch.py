#!/usr/bin/env python3
"""
Eurostar Snap watcher.

Loads Eurostar Snap search pages in a real (headless) browser for each route and
date, looks for prices on the page, and sends ONE alert per run listing anything
that has newly appeared since the last check. Tickets you've already been told
about are not re-alerted unless they disappear and later come back.

Notification channels (use either or both, configured via environment variables):
  NTFY_TOPIC                      -> free push notification to your phone via ntfy.sh
  GMAIL_ADDRESS + GMAIL_APP_PASSWORD (+ ALERT_EMAIL) -> email via Gmail

Other settings (all optional):
  DAYS_AHEAD=7          how many days ahead to check, starting today
  DATES=2026-11-20,...  check exactly these dates instead of DAYS_AHEAD
  DEBUG=1               save screenshots/HTML/JSON of every page into ./debug
  FAIL_ALERT_THRESHOLD  consecutive fully-failed runs before a "watcher blocked" alert (default 6)

Usage:
  python snap_watch.py            # one check (what GitHub Actions runs)
  python snap_watch.py --loop     # check forever, every INTERVAL_MIN minutes (default 5)
"""

import datetime as dt
import json
import os
import random
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- config ----

STATIONS = {
    "London St Pancras": "7015400",
    "Amsterdam Centraal": "8400058",
    "Rotterdam Centraal": "8400530",
}

ROUTES = [
    ("Rotterdam Centraal", "London St Pancras"),
    ("Amsterdam Centraal", "London St Pancras"),
    ("London St Pancras", "Rotterdam Centraal"),
    ("London St Pancras", "Amsterdam Centraal"),
]

SEARCH_URL = (
    "https://snap.eurostar.com/uk-en/search"
    "?adult=1&origin={origin}&destination={destination}&outbound={date}"
)

TZ = ZoneInfo("Europe/London")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD") or "7")
DATES_ENV = os.getenv("DATES", "")
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
DEBUG = os.getenv("DEBUG", "") not in ("", "0", "false", "False")
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "debug"))
FAIL_ALERT_THRESHOLD = int(os.getenv("FAIL_ALERT_THRESHOLD") or "6")
INTERVAL_MIN = float(os.getenv("INTERVAL_MIN") or "5")

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "").strip() or GMAIL_ADDRESS

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

PRICE_RE = re.compile(r"[£€]\s?(\d{1,4}(?:[.,]\d{2})?)")
BLOCK_MARKERS = (
    "access denied",
    "verify you are human",
    "are you a robot",
    "captcha",
    "request blocked",
    "pardon our interruption",
    "just a moment",
    "unusual traffic",
)


def log(msg):
    print(f"[{dt.datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ----------------------------------------------------------------- state ----

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("state.json was unreadable, starting fresh")
    return {}


def save_state(state, original):
    """Only write if something meaningful changed (keeps git history quiet)."""
    today = dt.datetime.now(TZ).date()
    last = state.get("keepalive")
    # GitHub disables scheduled workflows after 60 days without a commit,
    # so touch the file roughly monthly even if nothing else changed.
    if not last or (today - dt.date.fromisoformat(last)).days >= 25:
        state["keepalive"] = today.isoformat()
    new_text = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if new_text != original:
        STATE_FILE.write_text(new_text)
        log("state.json updated")


# ---------------------------------------------------------------- dates -----

def build_dates():
    if DATES_ENV.strip():
        return sorted({d.strip() for d in DATES_ENV.split(",") if d.strip()})
    today = dt.datetime.now(TZ).date()
    return [(today + dt.timedelta(days=i)).isoformat() for i in range(DAYS_AHEAD)]


def pretty_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


# -------------------------------------------------------------- scraping ----

def extract_offers(text):
    """
    Find every line on the page that shows a non-zero price, and keep the two
    lines above it as context (usually the time slot / class label).

    Returns {key: display_text}. The key has prices stripped out, so a price
    change for the same slot doesn't count as a 'new' ticket.
    """
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    offers = {}
    for i, line in enumerate(lines):
        prices = [m.group(1) for m in PRICE_RE.finditer(line)]
        if not prices:
            continue
        if all(float(p.replace(",", ".")) == 0 for p in prices):
            continue  # e.g. an empty basket showing "Total £0.00"
        context = " | ".join(lines[max(0, i - 2): i + 1])
        if len(context) > 200:
            context = context[:200] + "…"
        key = PRICE_RE.sub("£?", context).lower()
        offers[key] = context
    return offers


def save_debug(page, name, json_responses=None):
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
        (DEBUG_DIR / f"{name}.html").write_text(page.content())
        if json_responses:
            (DEBUG_DIR / f"{name}.responses.json").write_text(
                json.dumps(json_responses, indent=2, ensure_ascii=False)
            )
    except Exception as e:  # never let debugging break the run
        log(f"  (couldn't save debug files: {e})")


def check(page, origin, destination, date):
    url = SEARCH_URL.format(
        origin=STATIONS[origin], destination=STATIONS[destination], date=date
    )
    name = f"{date}_{origin.split()[0]}-{destination.split()[0]}"
    captured = []
    handler = captured.append
    if DEBUG:
        page.on("response", handler)

    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2_500)  # let the results render
        text = page.inner_text("body", timeout=10_000)
    except Exception as e:
        save_debug(page, name + "_ERROR")
        return {"status": "error", "detail": str(e)[:200], "url": url}
    finally:
        if DEBUG:
            page.remove_listener("response", handler)

    low = text.lower()
    code = resp.status if resp else None

    if (code and code >= 400) or any(m in low for m in BLOCK_MARKERS) or len(low) < 150:
        save_debug(page, name + "_BLOCKED")
        return {"status": "blocked", "detail": f"HTTP {code}", "url": url}

    offers = extract_offers(text)

    if DEBUG:
        bodies = []
        for r in captured:
            try:
                if "json" in (r.headers.get("content-type") or ""):
                    bodies.append({"url": r.url, "status": r.status, "body": r.text()[:20_000]})
            except Exception:
                pass
        save_debug(page, name, bodies)
        (DEBUG_DIR / f"{name}.txt").write_text(text)

    return {"status": "ok", "offers": offers, "url": url}


# --------------------------------------------------------------- notify -----

def send_ntfy(title, body, click_url=None):
    if not NTFY_TOPIC:
        return False
    headers = {"Title": title, "Priority": "high", "Tags": "train"}
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
        log("  ntfy push sent")
        return True
    except Exception as e:
        log(f"  ntfy failed: {e}")
        return False


def send_email(subject, body):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and ALERT_EMAIL):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ALERT_EMAIL
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.send_message(msg)
        log(f"  email sent to {ALERT_EMAIL}")
        return True
    except Exception as e:
        log(f"  email failed: {e}")
        return False


def notify(title, body, click_url=None):
    sent = send_ntfy(title, body, click_url)
    sent = send_email(title, body) or sent
    if not sent:
        log("No notification channel configured (or all failed). Message was:")
        print(f"--- {title} ---\n{body}\n---", flush=True)


def format_finds(finds):
    by_search = {}
    for f in finds:
        by_search.setdefault((f["route"], f["date"], f["url"]), []).append(f["text"])
    parts = []
    for (route, date, url), texts in sorted(by_search.items(), key=lambda x: (x[0][1], x[0][0])):
        parts.append(f"{route}, {pretty_date(date)}")
        parts.extend(f"  • {t}" for t in texts)
        parts.append(f"  Book: {url}")
        parts.append("")
    return "\n".join(parts).strip()


# ----------------------------------------------------------------- main -----

def run_once():
    state = load_state()
    original = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    prev_active = state.get("active", {})
    active = dict(prev_active)
    dates = build_dates()

    finds, checks, failures = [], 0, 0
    log(f"Checking {len(ROUTES)} routes x {len(dates)} dates ({dates[0]} … {dates[-1]})")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        for origin, destination in ROUTES:
            route = f"{origin} → {destination}"
            for date in dates:
                res = check(page, origin, destination, date)
                checks += 1
                prefix = f"{origin}>{destination}|{date}|"

                if res["status"] != "ok":
                    failures += 1
                    log(f"  {route} {date}: {res['status'].upper()} ({res['detail']})")
                    # Don't touch remembered state on a failed load, otherwise
                    # we'd re-alert the same tickets once it loads again.
                else:
                    for k in [k for k in active if k.startswith(prefix)]:
                        del active[k]
                    for key, text in res["offers"].items():
                        full = prefix + key
                        active[full] = {"route": route, "date": date, "text": text, "url": res["url"]}
                        if full not in prev_active:
                            finds.append(active[full])
                    log(f"  {route} {date}: {len(res['offers'])} priced item(s) on page")

                time.sleep(random.uniform(1.5, 4.0))  # be polite

        browser.close()

    # forget anything for dates already gone
    today = dt.datetime.now(TZ).date().isoformat()
    active = {k: v for k, v in active.items() if v["date"] >= today}
    state["active"] = active

    if finds:
        n = len(finds)
        title = f"Eurostar Snap: {n} new ticket option{'s' if n != 1 else ''}"
        body = format_finds(finds)
        log(title)
        notify(title, body, click_url=finds[0]["url"])
    else:
        log("Nothing new.")

    # --- health monitoring: tell me if the watcher itself is broken ---------
    if checks and failures == checks:
        state["failed_runs"] = state.get("failed_runs", 0) + 1
        if state["failed_runs"] >= FAIL_ALERT_THRESHOLD and not state.get("fail_alert_sent"):
            notify(
                "Eurostar Snap watcher is failing",
                f"The last {state['failed_runs']} runs couldn't load any Snap pages "
                "(probably bot protection or the site changed). You may be missing "
                "tickets. Check the GitHub Actions logs / debug artifacts.",
            )
            state["fail_alert_sent"] = True
    else:
        if state.get("fail_alert_sent"):
            notify("Eurostar Snap watcher is working again", "Pages are loading normally again.")
        state["failed_runs"] = 0
        state["fail_alert_sent"] = False

    save_state(state, original)


def main():
    if "--loop" in sys.argv:
        while True:
            try:
                run_once()
            except Exception as e:
                log(f"Run crashed: {e}")
            wait = INTERVAL_MIN * 60 + random.uniform(-30, 30)
            log(f"Sleeping {wait/60:.1f} min")
            time.sleep(max(60, wait))
    else:
        run_once()


if __name__ == "__main__":
    main()
