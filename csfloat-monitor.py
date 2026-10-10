#!/usr/bin/env python3
"""
CSFloat listing monitor (undetected-chromedriver)
Enhanced for macOS stability, stealth, and memory management.
"""

import csv
import json
import random
import re
import sys
import os
import time
from datetime import datetime
from pathlib import Path
from urllib import request as urlrequest

from dotenv import load_dotenv
import undetected_chromedriver as uc
from selenium.common.exceptions import (
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

# Load environment variables from .env file
load_dotenv()

# ============================== CONFIG ==============================

TARGET_URL = os.getenv("TARGET_URL", "https://csfloat.com")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

try:
    MAX_PRICE = float(os.getenv("MAX_PRICE")) if os.getenv("MAX_PRICE") else None
except ValueError:
    MAX_PRICE = None

try:
    MAX_FLOAT = float(os.getenv("MAX_FLOAT")) if os.getenv("MAX_FLOAT") else None
except ValueError:
    MAX_FLOAT = None

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 15))
INTERVAL_JITTER = 5        # +/- random seconds
TOP_N = 10                 # how many of the first listings to inspect each round
PAGE_LOAD_TIMEOUT = 25     # seconds to wait for listing cards to appear
ALERT_ON_FIRST_RUN = False # False: first scan only records current listings as "seen"

CHROME_VERSION = 153       # Locked to your macOS Chrome version for stability
HEADLESS = False           
BLOCK_IMAGES = False       

ENABLE_SOUND = True
ENABLE_DESKTOP_NOTIFICATION = True   

MAX_BROWSER_RESTARTS = 10  

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "chrome_profile"
SEEN_FILE = BASE_DIR / "seen_listings.json"
LOG_FILE = BASE_DIR / "found_listings.csv"

# ====================================================================

PRICE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{1,2})?)")
FLOAT_RE = re.compile(r"\b0\.\d{4,}\b")
BLOCK_TITLES = ("just a moment", "attention required", "access denied")

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

# ---------------------------- persistence ----------------------------

def load_seen() -> set:
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen(seen: set) -> None:
    items = list(seen)[-2000:]
    SEEN_FILE.write_text(json.dumps(items), encoding="utf-8")

def append_csv(row: dict) -> None:
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["time", "name", "price", "float", "url"])
        if new_file:
            writer.writeheader()
        writer.writerow(row)

# ------------------------------ alerts -------------------------------

def beep() -> None:
    if not ENABLE_SOUND:
        return
    
    if sys.platform == "darwin":  # macOS native sound
        os.system("afplay /System/Library/Sounds/Glass.aiff &")
    elif sys.platform == "win32": # Windows
        try:
            import winsound
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

def discord_notify(listing: dict) -> None:
    if not DISCORD_WEBHOOK_URL:
        return
    price = f"${listing['price']:.2f}" if listing["price"] is not None else "?"
    flt = listing["float"] or "?"
    payload = {
        "content": f"🆕 **{listing['name']}**\n💰 {price}   🎯 float {flt}\n{listing['url'] or ''}"
    }
    try:
        req = urlrequest.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "csfloat-monitor"},
        )
        urlrequest.urlopen(req, timeout=10).close()
    except Exception as e:
        log(f"⚠️ Discord webhook failed: {e}")

def alert(listing: dict) -> None:
    price = f"${listing['price']:.2f}" if listing["price"] is not None else "?"
    log(f"🆕 {listing['name'][:45].ljust(45)} | 💰 {price:>9} | 🎯 {listing['float'] or '-'}")
    if listing["url"]:
        print(f"      🔗 {listing['url']}")
    beep()
    desktop_notify("New CSFloat listing", f"{listing['name']} - {price}")
    discord_notify(listing)
    append_csv({
        "time": datetime.now().isoformat(timespec="seconds"),
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

def parse_card(raw: dict) -> dict | None:
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

def passes_filters(listing: dict) -> bool:
    if MAX_PRICE is not None and listing["price"] is not None and listing["price"] > MAX_PRICE:
        return False
    if MAX_FLOAT is not None and listing["float"] is not None and float(listing["float"]) > MAX_FLOAT:
        return False
    return True

# ------------------------------ main loop ------------------------------

def monitor(driver, seen: set, first_run: bool) -> None:
    log(f"🌐 Opening: {TARGET_URL}")
    driver.get(TARGET_URL)
    
    # Initial wait to pass Cloudflare on fresh loads
    time.sleep(10)

    backoff = 0
    rounds = 0
    total_new = 0

    while True:
        rounds += 1
        
        # Memory leak prevention
        if rounds > 500:
            log("🧹 Proactive restart to clear Chrome's memory...")
            return

        # Stealth mechanics: mix of refresh and hard re-navigation
        if rounds > 1:
            if random.random() < 0.10:
                driver.get(TARGET_URL)
            else:
                driver.refresh()

        if is_blocked(driver):
            backoff = min(max(backoff * 2, 30), 600)
            log(f"🛑 Looks like a Cloudflare/anti-bot page. Solve it in the window if needed. Waiting {backoff}s...")
            time.sleep(backoff)
            continue

        if not wait_for_cards(driver):
            backoff = min(max(backoff * 2, 15), 300)
            log(f"⚠️ No listing cards found (slow load, no results, or blocked). Retrying in {backoff}s...")
            time.sleep(backoff)
            continue

        backoff = 0
        raw_cards = driver.execute_script(JS_GRAB_CARDS, TOP_N)
        listings = [p for p in (parse_card(r) for r in raw_cards) if p]

        new_found = 0
        for listing in reversed(listings):
            if listing["id"] in seen:
                continue
            seen.add(listing["id"])
            if first_run and not ALERT_ON_FIRST_RUN:
                continue
            if not passes_filters(listing):
                continue
            alert(listing)
            new_found += 1

        if first_run:
            log(f"📌 Baseline saved: {len(listings)} current listings marked as seen. Watching for new ones...")
            first_run = False
        elif new_found == 0:
            log(f"… no new listings (round {rounds}, {len(seen)} tracked)")

        # Save only when something actually changes to protect SSD lifespan
        if new_found > 0:
            total_new += new_found
            save_seen(seen)

        delay = max(5, CHECK_INTERVAL + random.uniform(-INTERVAL_JITTER, INTERVAL_JITTER))
        time.sleep(delay)

def main() -> None:
    print("🚀 CSFloat Monitor starting (Ctrl+C to stop)")
    PROFILE_DIR.mkdir(exist_ok=True)

    seen = load_seen()
    first_run = not seen
    restarts = 0
    driver = None

    try:
        while restarts <= MAX_BROWSER_RESTARTS:
            try:
                log("🔧 Starting browser...")
                driver = make_driver()
                monitor(driver, seen, first_run)
            except (NoSuchWindowException, WebDriverException) as e:
                restarts += 1
                short = str(e).splitlines()[0] if str(e) else type(e).__name__
                log(f"❌ Browser problem ({short}). Restart {restarts}/{MAX_BROWSER_RESTARTS} in 10s...")
                first_run = False
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