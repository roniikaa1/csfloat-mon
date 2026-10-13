#!/usr/bin/env python3
"""
CSFloat multi-target listing monitor (undetected-chromedriver)

Features
- Track MANY searches at once, each with its own filters / webhook
- Add, remove or disable targets while running via targets.json (hot-reloaded)
- Alerts only on NEW listings (persistent per-target "seen" cache)
- Sound, desktop notification and Discord webhook alerts
- CSV log of every new find (includes target name)
- Randomized refresh interval + per-target exponential backoff when blocked
- Auto-restarts the browser if it crashes or is closed
- Persistent Chrome profile (keeps cookies / Cloudflare clearance / login)

Install:  pip install undetected-chromedriver selenium
Optional: pip install plyer   (desktop notifications)

targets.json example (list of objects; only name + url are required):
[
  {
    "name": "AK Redline C393",
    "url": "https://csfloat.com/search?...",
    "max_price": 150,
    "max_float": 0.25,
    "enabled": true,
    "discord_webhook": ""
  }
]
"""

import csv
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import request as urlrequest

import undetected_chromedriver as uc
from selenium.common.exceptions import (
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

# ============================== CONFIG ==============================

# Used only if targets.json does not exist next to this script.
TARGETS = [
    {
        "name": "Target 1",
        "url": (
            "https://csfloat.com/search?sort_by=most_recent&min_float=0.06&max_float=0.8"
            "&type=buy_now&def_index=7&paint_index=14&stickers=%5B%7B%22c%22:%22C393%22%7D%5D"
        ),
        "max_price": None,       # e.g. 150.0
        "min_price": None,
        "max_float": None,       # e.g. 0.25
        "min_float": None,
        "enabled": True,
        "discord_webhook": "",   # optional: overrides the global webhook for this target
    },
]

CHECK_INTERVAL = 20         # base seconds between full rounds (all targets)
INTERVAL_JITTER = 5         # +/- random seconds
TARGET_PAUSE = (3, 8)       # random pause range between targets inside a round
TOP_N = 10                  # first N listings inspected per target
PAGE_LOAD_TIMEOUT = 25      # seconds to wait for listing cards

ALERT_ON_FIRST_RUN = False  # False: a target's first scan only records current listings

CHROME_VERSION = None       # None = auto-detect. Set e.g. 153 only if auto-detect fails
HEADLESS = False            # headless often gets blocked by Cloudflare
BLOCK_IMAGES = False

ENABLE_SOUND = True
ENABLE_DESKTOP_NOTIFICATION = True
DISCORD_WEBHOOK_URL = ""    # global webhook (used when a target has none)

MAX_BROWSER_RESTARTS = 10

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "chrome_profile"
SEEN_FILE = BASE_DIR / "seen_listings.json"
LOG_FILE = BASE_DIR / "found_listings.csv"
TARGETS_FILE = BASE_DIR / "targets.json"

# ====================================================================

PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")
FLOAT_RE = re.compile(r"\b0\.\d{4,}\b")
BLOCK_TITLES = ("just a moment", "attention required", "access denied")
SEEN_PER_TARGET = 1000

JS_GRAB_CARDS = """
return Array.from(document.querySelectorAll('item-card'))
  .slice(0, arguments[0])
  .map(c => {
    const a = c.querySelector("a[href*='/item/']");
    return {text: c.innerText || '', href: a ? a.href : null};
  });
"""


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


# ----------------------------- targets -------------------------------

_last_good_targets = None
_last_targets_mtime = None


def load_targets() -> list:
    """Returns enabled targets. Re-reads targets.json whenever it changes."""
    global _last_good_targets, _last_targets_mtime

    if not TARGETS_FILE.exists():
        raw = TARGETS
        source = "script config"
    else:
        mtime = TARGETS_FILE.stat().st_mtime
        if mtime == _last_targets_mtime and _last_good_targets is not None:
            return _last_good_targets
        _last_targets_mtime = mtime
        try:
            raw = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("top level must be a list")
            source = "targets.json"
        except (json.JSONDecodeError, ValueError, OSError) as e:
            log(f"⚠️ Could not read targets.json ({e}). Keeping previous targets.")
            return _last_good_targets or []

    targets, names = [], set()
    for t in raw:
        if not isinstance(t, dict) or not t.get("name") or not t.get("url"):
            log(f"⚠️ Skipping target without name/url: {str(t)[:80]}")
            continue
        if t["name"] in names:
            log(f"⚠️ Duplicate target name '{t['name']}' skipped (names must be unique)")
            continue
        names.add(t["name"])
        if t.get("enabled", True):
            targets.append(t)

    if _last_good_targets is None or [x["name"] for x in targets] != [x["name"] for x in _last_good_targets]:
        log(f"🎯 Loaded {len(targets)} active target(s) from {source}: "
            + ", ".join(t["name"] for t in targets))
    _last_good_targets = targets
    return targets


# ---------------------------- persistence ----------------------------

def load_seen() -> dict:
    """seen[target_name] = {listing_id: None, ...} (insertion-ordered)."""
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: dict.fromkeys(v) for k, v in data.items() if isinstance(v, list)}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_seen(seen: dict) -> None:
    out = {k: list(v)[-SEEN_PER_TARGET:] for k, v in seen.items()}
    SEEN_FILE.write_text(json.dumps(out), encoding="utf-8")


def append_csv(row: dict) -> None:
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["time", "target", "name", "price", "float", "url"])
        if new_file:
            writer.writeheader()
        writer.writerow(row)


# ------------------------------ alerts -------------------------------

def beep() -> None:
    if not ENABLE_SOUND:
        return
    try:
        import winsound  # Windows only
        winsound.Beep(1200, 250)
        winsound.Beep(1600, 250)
    except ImportError:
        print("\a", end="", flush=True)


def desktop_notify(title: str, message: str) -> None:
    if not ENABLE_DESKTOP_NOTIFICATION:
        return
    try:
        from plyer import notification
        notification.notify(title=title, message=message, timeout=8)
    except Exception:
        pass


def discord_notify(target: dict, listing: dict) -> None:
    webhook = target.get("discord_webhook") or DISCORD_WEBHOOK_URL
    if not webhook:
        return
    price = f"${listing['price']:.2f}" if listing["price"] is not None else "?"
    flt = listing["float"] or "?"
    payload = {
        "content": (f"🆕 **[{target['name']}] {listing['name']}**\n"
                    f"💰 {price}   🎯 float {flt}\n{listing['url'] or ''}")
    }
    try:
        req = urlrequest.Request(
            webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "csfloat-monitor"},
        )
        urlrequest.urlopen(req, timeout=10).close()
    except Exception as e:
        log(f"⚠️ Discord webhook failed: {e}")


def alert(target: dict, listing: dict) -> None:
    price = f"${listing['price']:.2f}" if listing["price"] is not None else "?"
    log(f"🆕 [{target['name'][:18]}] {listing['name'][:40].ljust(40)} | 💰 {price:>9} | 🎯 {listing['float'] or '-'}")
    if listing["url"]:
        print(f"      🔗 {listing['url']}")
    beep()
    desktop_notify(f"New: {target['name']}", f"{listing['name']} - {price}")
    discord_notify(target, listing)
    append_csv({
        "time": datetime.now().isoformat(timespec="seconds"),
        "target": target["name"],
        "name": listing["name"],
        "price": listing["price"],
        "float": listing["float"],
        "url": listing["url"],
    })


# ------------------------------ browser ------------------------------

def make_driver():
    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    if HEADLESS:
        options.add_argument("--headless=new")
    if BLOCK_IMAGES:
        options.add_argument("--blink-settings=imagesEnabled=false")

    kwargs = {"options": options}
    if CHROME_VERSION:
        kwargs["version_main"] = CHROME_VERSION
    return uc.Chrome(**kwargs)


def is_blocked(driver) -> bool:
    title = (driver.title or "").lower()
    return any(t in title for t in BLOCK_TITLES)


def wait_for_cards(driver) -> bool:
    try:
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, "item-card")
        )
        return True
    except TimeoutException:
        return False


# ------------------------------ parsing ------------------------------

def parse_card(raw: dict):
    text = (raw.get("text") or "").strip()
    if not text:
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    name = next((ln for ln in lines if " | " in ln), lines[0])

    price_match = PRICE_RE.search(text)
    price = float(price_match.group(1).replace(",", "")) if price_match else None

    float_match = FLOAT_RE.search(text)
    flt = float_match.group(0) if float_match else None

    url = raw.get("href")
    uid = url or f"{name}|{price}|{flt}"
    return {"id": uid, "name": name, "price": price, "float": flt, "url": url}


def passes_filters(target: dict, listing: dict) -> bool:
    p, f = listing["price"], listing["float"]
    if p is not None:
        if target.get("max_price") is not None and p > target["max_price"]:
            return False
        if target.get("min_price") is not None and p < target["min_price"]:
            return False
    if f is not None:
        fv = float(f)
        if target.get("max_float") is not None and fv > target["max_float"]:
            return False
        if target.get("min_float") is not None and fv < target["min_float"]:
            return False
    return True


# ------------------------------ scanning ------------------------------

def scan_target(driver, target: dict, seen: dict) -> str:
    """Returns 'ok', 'blocked' or 'empty'."""
    name = target["name"]
    first_run = name not in seen
    seen_t = seen.setdefault(name, {})

    driver.get(target["url"])

    if is_blocked(driver):
        return "blocked"
    if not wait_for_cards(driver):
        return "empty"

    raw_cards = driver.execute_script(JS_GRAB_CARDS, TOP_N)
    listings = [p for p in (parse_card(r) for r in raw_cards) if p]

    new_found = 0
    for listing in reversed(listings):  # oldest -> newest
        if listing["id"] in seen_t:
            continue
        seen_t[listing["id"]] = None
        if first_run and not (ALERT_ON_FIRST_RUN or target.get("alert_on_first_run")):
            continue
        if not passes_filters(target, listing):
            continue
        alert(target, listing)
        new_found += 1

    if first_run:
        log(f"📌 [{name}] Baseline saved: {len(listings)} listings marked as seen.")
    elif new_found == 0:
        log(f"… [{name}] no new listings ({len(seen_t)} tracked)")
    return "ok"


def monitor(driver, seen: dict) -> None:
    """Runs until the browser dies or the user stops the script."""
    backoff = {}       # target name -> current backoff seconds
    retry_at = {}      # target name -> epoch time when we may try again
    rounds = 0

    while True:
        rounds += 1
        targets = load_targets()
        if not targets:
            log("💤 No active targets. Edit targets.json — checking again in 10s...")
            time.sleep(10)
            continue

        random.shuffle(targets)  # don't always hit the same one first
        for i, target in enumerate(targets):
            name = target["name"]
            if time.time() < retry_at.get(name, 0):
                continue

            status = scan_target(driver, target, seen)
            if status == "ok":
                backoff[name] = 0
            elif status == "blocked":
                backoff[name] = min(max(backoff.get(name, 0) * 2, 30), 600)
                log(f"🛑 [{name}] Cloudflare/anti-bot page. Solve it in the window if needed. "
                    f"Skipping this target for {backoff[name]}s.")
                retry_at[name] = time.time() + backoff[name]
            else:
                backoff[name] = min(max(backoff.get(name, 0) * 2, 15), 300)
                log(f"⚠️ [{name}] No cards (slow load, no results, or blocked). "
                    f"Retrying in {backoff[name]}s.")
                retry_at[name] = time.time() + backoff[name]

            save_seen(seen)
            if i < len(targets) - 1:
                time.sleep(random.uniform(*TARGET_PAUSE))

        delay = max(5, CHECK_INTERVAL + random.uniform(-INTERVAL_JITTER, INTERVAL_JITTER))
        time.sleep(delay)


def main() -> None:
    print("🚀 CSFloat Multi-Target Monitor starting (Ctrl+C to stop)")
    PROFILE_DIR.mkdir(exist_ok=True)

    seen = load_seen()
    restarts = 0
    driver = None

    try:
        while restarts <= MAX_BROWSER_RESTARTS:
            try:
                log("🔧 Starting browser...")
                driver = make_driver()
                monitor(driver, seen)
            except (NoSuchWindowException, WebDriverException) as e:
                restarts += 1
                short = str(e).splitlines()[0] if str(e) else type(e).__name__
                log(f"❌ Browser problem ({short}). Restart {restarts}/{MAX_BROWSER_RESTARTS} in 10s...")
                time.sleep(10)
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = None

        log("⛔ Too many browser failures. Stopping.")
    except KeyboardInterrupt:
        print("\n🛑 Stopped by user. Bye!")
    finally:
        save_seen(seen)


if __name__ == "__main__":
    main()
    sys.exit(0)
