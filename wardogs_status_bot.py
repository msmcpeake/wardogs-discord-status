"""Watches for the Wardogs pause-menu server info on screen and mirrors it
into a single Discord message that gets edited in place (in a channel where
only the bot can post, so it's always the one message there). Editing avoids
Discord's much stricter per-channel rename rate limit (~2/10min) and doesn't
spam notifications the way a new message each time would.

Setup: copy .env.example to .env and fill in DISCORD_BOT_TOKEN and
DISCORD_STATUS_CHANNEL_ID (see README.md for how to get both).

Usage:
    python wardogs_status_bot.py            run with a system tray icon
                                              (this is what runs hidden at
                                              login - see README "Run in
                                              the background")
    python wardogs_status_bot.py --once      capture+OCR+parse once and print
                                              the result (no Discord call,
                                              no game-running check) - use
                                              this while sitting in the
                                              pause menu to tune CAPTURE_REGION
    python wardogs_status_bot.py --dry-run   run continuously in the console,
                                              logging what it would do instead
                                              of calling Discord
    python wardogs_status_bot.py --no-tray   run continuously in the console,
                                              calling Discord as normal, but
                                              without the tray icon
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone

import mss
import psutil
import pystray
import pytesseract
import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Explicit path (rather than load_dotenv()'s cwd-based search) so this still
# finds .env when launched from a different working directory - e.g. Task
# Scheduler, which doesn't run with SCRIPT_DIR as the current directory.
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

STATE_FILE = os.path.join(SCRIPT_DIR, "last_status.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "wardogs_status.log")
TRAY_ICON_FILE = os.path.join(SCRIPT_DIR, "tray_icon.png")

TESSERACT_CMD = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_STATUS_CHANNEL_ID = os.getenv("DISCORD_STATUS_CHANNEL_ID")
GAME_PROCESS_SUBSTRING = os.getenv("GAME_PROCESS_SUBSTRING", "wardogs")
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "4"))

# Fraction of the screen to crop before OCR: "left,top,right,bottom" as 0-1
# fractions. Default covers the bottom ~35% of the screen: the bottom-right
# pause-menu panel (CURRENT SERVER / SERVER ID), the bottom-center "PRESS
# ANY BUTTON TO START" splash text, and the bottom-left DEPLOY / SERVER
# BROWSER button on the main menu - all measured to sit within this band.
# Smaller region = less for Tesseract to process = faster polling, so widen
# this only as far as you actually need to if something isn't being found.
CAPTURE_REGION = os.getenv("CAPTURE_REGION", "0,0.65,1.0,1.0")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("wardogs-status")

NAME_RE = re.compile(r"CURRENT\s*SERVER[:.\s]*(.+)", re.IGNORECASE)
# [I1l] tolerates "ID" getting OCR'd as "1D" (seen in practice), same idea
# as the [0-9OolI] digit class below tolerating O/0 and l/I/1 mixups.
ID_RE = re.compile(r"SERVER\s*[I1l]D[^0-9OolI]*([0-9OolI]{3}\s*-\s*[0-9OolI]{3})", re.IGNORECASE)
REGION_RE = re.compile(r"\(([^)|#]+)\)?")
NUM_RE = re.compile(r"#\s*(\d+)")
# Markers for "not actually in a match": the "EARLY ACCESS" watermark on the
# main menu / server browser (version number deliberately excluded, so this
# keeps matching across game updates), and the "PRESS ANY BUTTON TO START"
# splash screen shown before the main menu. Anchored on "ANY BUTTON TO
# START" rather than including "PRESS" - that busy photo background makes
# Tesseract's PSM 6 fallback (see capture_and_parse) merge "PRESS" with
# adjacent image noise in practice, while the rest stays intact.
MENU_RE = re.compile(r"EARLY\s*ACCESS|ANY\s*BUTTON\s*TO\s*START", re.IGNORECASE)
# DEPLOY alone isn't safe (many shooters show a "REDEPLOY" prompt after
# dying mid-match) - require it together with "SERVER BROWSER" (the
# subtitle under the same button on the main menu) to be specific enough.
DEPLOY_RE = re.compile(r"DEPLOY", re.IGNORECASE)
SERVER_BROWSER_RE = re.compile(r"SERVER\s*BROWSER", re.IGNORECASE)

NOT_IN_GAME = "Matrix is not in a game"


def is_game_running() -> bool:
    needle = GAME_PROCESS_SUBSTRING.lower()
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if needle in name:
            return True
    return False


def grab_region() -> Image.Image:
    left_f, top_f, right_f, bottom_f = (float(x) for x in CAPTURE_REGION.split(","))
    with mss.MSS() as sct:
        mon = sct.monitors[1]
        w, h = mon["width"], mon["height"]
        box = {
            "left": mon["left"] + int(w * left_f),
            "top": mon["top"] + int(h * top_f),
            "width": int(w * (right_f - left_f)),
            "height": int(h * (bottom_f - top_f)),
        }
        shot = sct.grab(box)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def preprocess(img: Image.Image) -> Image.Image:
    # No upscaling here: on this high-res capture it roughly quadrupled the
    # pixels Tesseract has to process (a large chunk of per-poll CPU time),
    # and a direct comparison found it detected LESS text, not more - so it
    # was pure overhead with no accuracy benefit on a display this sharp.
    # Only reintroduce upscaling if real gameplay testing shows misreads on
    # a lower-resolution display.
    img = img.convert("L")
    return ImageOps.autocontrast(img)


def parse_server(text: str):
    """Returns the formatted status, or None if the OCR text doesn't contain
    a complete reading (region, number, AND server id). Requiring all three
    avoids treating a partial/flaky OCR pass (e.g. one that misses the
    SERVER ID line) as a genuinely different status."""
    name_match = NAME_RE.search(text)
    if not name_match:
        return None
    full_name = name_match.group(1).strip()

    region_match = REGION_RE.search(full_name)
    num_match = NUM_RE.search(full_name)
    id_match = ID_RE.search(text)
    if not (region_match and num_match and id_match):
        return None
    region = region_match.group(1).strip()
    num = num_match.group(1).strip()

    fixed = id_match.group(1).translate(str.maketrans("OolI", "0011"))
    server_id = re.sub(r"\s*-\s*", "-", fixed)

    return f"{region} #{num} \u00b7 ID {server_id}"


def determine_status(text: str):
    """Returns a server status, NOT_IN_GAME, or None (ambiguous - e.g.
    actively playing with the pause menu closed, where neither the server
    panel nor the menu watermark is visible; leave whatever status is
    already showing alone rather than guessing)."""
    status = parse_server(text)
    if status:
        return status
    if MENU_RE.search(text):
        return NOT_IN_GAME
    if DEPLOY_RE.search(text) and SERVER_BROWSER_RE.search(text):
        return NOT_IN_GAME
    return None


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("status"), data.get("message_id")
        except (OSError, json.JSONDecodeError):
            return None, None
    return None, None


def save_state(status: str, message_id: str):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"status": status, "message_id": message_id, "updated_at": datetime.now(timezone.utc).isoformat()}, f
        )


def _discord_headers():
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (https://github.com, 1.0) wardogs-status-bot",
    }


EMBED_COLOR_IN_GAME = 0x57F287  # Discord green
EMBED_COLOR_NOT_IN_GAME = 0x99AAB5  # Discord grey


STATUS_EMOJI = "<:blue:1548650924782653561>"  # :blue: from the Wardogs Discord, re-uploaded into Milk Cult
                                               # since bots can only render custom emoji from guilds they're in


def _build_embed(text: str):
    description = text if text == NOT_IN_GAME else f"{STATUS_EMOJI} │ {text}"
    return {
        "title": "Current Wardogs Server",
        "description": description,
        "color": EMBED_COLOR_NOT_IN_GAME if text == NOT_IN_GAME else EMBED_COLOR_IN_GAME,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Last updated"},
    }


def set_status_message(text: str, message_id: str | None):
    """Creates the status message on first use, then edits it in place on
    every later call. Returns (success, retry_after_seconds_or_None,
    message_id). Never blocks on rate limits itself - the caller decides
    what to do while waiting."""
    headers = _discord_headers()
    # content is explicitly cleared so editing an older plain-text message
    # (from before embeds were added) doesn't leave stale text above the embed.
    body = {"content": "", "embeds": [_build_embed(text)]}

    if message_id:
        url = f"https://discord.com/api/v10/channels/{DISCORD_STATUS_CHANNEL_ID}/messages/{message_id}"
        resp = requests.patch(url, headers=headers, json=body, timeout=10)
    else:
        url = f"https://discord.com/api/v10/channels/{DISCORD_STATUS_CHANNEL_ID}/messages"
        resp = requests.post(url, headers=headers, json=body, timeout=10)

    if resp.status_code == 429:
        retry_after = resp.json().get("retry_after", 5)
        log.warning("Rate limited by Discord. Will hold off sending updates for %.1fs.", retry_after)
        return False, retry_after, message_id
    if resp.ok:
        new_id = resp.json()["id"]
        log.info("Status message %s to: %s", "updated" if message_id else "created", text)
        return True, None, new_id
    log.error("Failed to update status message (%s): %s", resp.status_code, resp.text)
    return False, None, message_id


def capture_and_parse():
    img = preprocess(grab_region())
    text = pytesseract.image_to_string(img)
    if not text.strip():
        # Default page segmentation (--psm 3, full automatic layout
        # analysis) can give up entirely on a busy/noisy background - seen
        # on the "PRESS ANY BUTTON TO START" splash, which sits over a
        # detailed rendered scene. --psm 6 is slower but far more reliable
        # there, so it's only worth paying for as a fallback when the fast
        # pass found nothing at all.
        text = pytesseract.image_to_string(img, config="--psm 6")
    return text, determine_status(text)


def run_once():
    text, status = capture_and_parse()
    print("--- raw OCR text ---")
    print(text)
    print("--- parsed status ---")
    print(status if status else "(no match - see README troubleshooting)")


def run_loop(dry_run: bool, stop_event: threading.Event | None = None, on_status=None):
    if not dry_run and (not DISCORD_BOT_TOKEN or not DISCORD_STATUS_CHANNEL_ID):
        log.error("Set DISCORD_BOT_TOKEN and DISCORD_STATUS_CHANNEL_ID in .env, or pass --dry-run.")
        sys.exit(1)

    if stop_event is None:
        stop_event = threading.Event()  # never set - just lets the loop below use one code path

    last_status, message_id = load_state()
    log.info("Watching for '%s' process. Last known status: %s", GAME_PROCESS_SUBSTRING, last_status)
    if on_status:
        on_status(last_status)

    # Debounce: only act on a reading once it's been seen twice in a row,
    # so a single flaky OCR pass can't trigger a spurious update.
    pending_status = None
    pending_count = 0
    cooldown_until = 0.0
    game_was_running = False

    def apply(status):
        nonlocal last_status, message_id, cooldown_until
        if time.time() < cooldown_until:
            return  # still cooling down from a rate limit, try again later
        if dry_run:
            log.info("[dry-run] would set status message to: %s", status)
            last_status = status
        else:
            ok, retry_after, message_id = set_status_message(status, message_id)
            if ok:
                last_status = status
                save_state(status, message_id)
            elif retry_after:
                cooldown_until = time.time() + retry_after
                return
            else:
                return
        if on_status:
            on_status(last_status)

    while not stop_event.is_set():
        try:
            running = is_game_running()
            if running:
                _, status = capture_and_parse()
                if status == pending_status:
                    pending_count += 1
                else:
                    pending_status = status
                    pending_count = 1

                if status and pending_count >= 2 and status != last_status:
                    apply(status)
            else:
                pending_status = None
                pending_count = 0
                if game_was_running and last_status != NOT_IN_GAME:
                    # Game just closed - this is a certain signal (not a
                    # flaky OCR read), so no need to debounce it.
                    apply(NOT_IN_GAME)
            game_was_running = running
        except KeyboardInterrupt:
            log.info("Stopping.")
            break
        except Exception:
            log.exception("Unexpected error, continuing")
        if stop_event.wait(POLL_INTERVAL_SECONDS):
            log.info("Stopping.")
            break


def run_tray(dry_run: bool):
    stop_event = threading.Event()
    icon_ref = {}

    def on_status(status):
        icon = icon_ref.get("icon")
        if icon:
            icon.title = f"Wardogs Status: {status}"[:127]  # tray tooltips have an OS-level length limit

    def on_quit(icon, _item):
        log.info("Quit requested from tray icon.")
        stop_event.set()
        icon.stop()

    def setup(icon):
        # pystray only pushes title updates to the OS once icon.visible is
        # True, and (per its docs) a custom setup callback like this one is
        # responsible for setting that itself. Doing that before starting
        # the OCR thread guarantees on_status's title updates always land,
        # instead of racing the tray icon's own startup.
        icon.visible = True
        thread = threading.Thread(
            target=run_loop, kwargs={"dry_run": dry_run, "stop_event": stop_event, "on_status": on_status}, daemon=True
        )
        thread.start()
        icon_ref["thread"] = thread

    image = Image.open(TRAY_ICON_FILE)
    menu = pystray.Menu(pystray.MenuItem("Quit", on_quit))
    icon = pystray.Icon("wardogs_status", image, "Wardogs Status: starting...", menu)
    icon_ref["icon"] = icon

    icon.run(setup=setup)  # blocks until icon.stop() is called
    thread = icon_ref.get("thread")
    if thread:
        thread.join(timeout=POLL_INTERVAL_SECONDS + 5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="capture+OCR+parse once and print, then exit")
    parser.add_argument("--dry-run", action="store_true", help="run continuously in the console, never calling Discord")
    parser.add_argument("--no-tray", action="store_true", help="run continuously in the console instead of showing a tray icon")
    args = parser.parse_args()

    if args.once:
        run_once()
    elif args.dry_run or args.no_tray:
        run_loop(dry_run=args.dry_run)
    else:
        run_tray(dry_run=False)
