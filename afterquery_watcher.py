import time
import logging
import requests
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY")
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 60))
# ─────────────────────────────────────────────────────────

AVAILABLE_URL = "https://experts.afterquery.com/api/projects/rewrite/tasks/available"
CLAIM_URL = "https://experts.afterquery.com/api/projects/rewrite/tasks/claim"
REFRESH_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("afterquery_watcher.log"),
    ],
)
log = logging.getLogger(__name__)

auth_token = None
session = requests.Session()
PROXY = os.environ.get("PROXY_URL")
session.proxies = {
    "http": PROXY,
    "https": PROXY,
}


def refresh_auth_token():
    global auth_token
    log.info("Refreshing auth token...")
    r = session.post(REFRESH_URL, json={
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
    }, timeout=10)
    r.raise_for_status()
    data = r.json()
    auth_token = data["id_token"]
    log.info(f"Token refreshed. Expires in {int(data.get('expires_in', 3600)) // 60} minutes.")


def get_headers():
    return {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
        "Origin": "https://experts.afterquery.com",
        "Referer": "https://experts.afterquery.com/projects/rewrite",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }


def send_discord(message, color=0x5865F2):
    payload = {
        "embeds": [{
            "title": "🤖 AfterQuery Watcher",
            "description": message,
            "color": color,
            "footer": {"text": f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"},
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK, json=payload, timeout=5).raise_for_status()
    except Exception as e:
        log.error(f"Discord error: {e}")


def check_available():
    r = session.get(AVAILABLE_URL, headers=get_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def claim_task():
    """Try to claim a task up to 3 times instantly."""
    for attempt in range(3):
        try:
            r = session.post(CLAIM_URL, headers=get_headers(), timeout=10)
            r.raise_for_status()
            result = r.json()
            if result.get("task"):
                return result
            log.warning(f"Claim attempt {attempt + 1} returned null. Retrying...")
            time.sleep(0.5)
        except Exception as e:
            log.error(f"Claim attempt {attempt + 1} failed: {e}")
            time.sleep(0.5)
    return None


def run():
    try:
        refresh_auth_token()
    except Exception as e:
        log.error(f"Failed to refresh token on startup: {e}")
        send_discord("❌ Failed to refresh token on startup.", color=0xED4245)
        return

    send_discord(
        f"🚀 **Watcher started!** Checking every {POLL_INTERVAL} seconds.",
        color=0xFEE75C
    )

    next_refresh = time.time() + 55 * 60

    # 403-specific tracking — separate from general errors
    consecutive_403s = 0
    MAX_403_RETRIES = 3          # after this many, stop refreshing and wait
    FORBIDDEN_WAIT = 5 * 60      # wait 5 minutes when 403 persists

    # General error tracking
    consecutive_errors = 0
    error_notified = False
    in_error_state = False
    backoff = POLL_INTERVAL
    MAX_BACKOFF = 10 * 60

    while True:
        try:
            # Proactive token refresh every 55 minutes
            if time.time() >= next_refresh:
                refresh_auth_token()
                next_refresh = time.time() + 55 * 60

            data = check_available()
            count = data.get("availableCount", 0)
            log.info(f"Available tasks: {count}")

            # Reset all error counters on success
            consecutive_403s = 0
            consecutive_errors = 0
            backoff = POLL_INTERVAL

            if in_error_state:
                in_error_state = False
                error_notified = False
                send_discord("✅ **Connection restored.** Back to monitoring.", color=0x57F287)

            if count > 0:
                log.info("🔥 Task available! Claiming immediately...")
                send_discord("🔥 **Task available!** Claiming now...", color=0xFEE75C)

                result = claim_task()

                if result and result.get("task"):
                    task = result["task"]
                    task_id = task.get("id", "unknown")
                    log.info(f"✅ Claimed task {task_id}!")
                    send_discord(
                        f"✅ **Task claimed!**\nTask ID: `{task_id}`\n[Open AfterQuery](https://experts.afterquery.com/projects/rewrite)",
                        color=0x57F287,
                    )
                else:
                    log.warning("Someone else claimed it first.")
                    send_discord("⚡ Task was available but someone else got it first.", color=0xED4245)

                # Check again immediately
                continue

            time.sleep(POLL_INTERVAL)

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"

            if status == 403:
                consecutive_403s += 1
                log.warning(f"403 Forbidden (consecutive: {consecutive_403s})")

                if consecutive_403s <= MAX_403_RETRIES:
                    # Try token refresh first
                    try:
                        refresh_auth_token()
                        next_refresh = time.time() + 55 * 60
                        log.info("Token refreshed after 403. Waiting 10s before retry...")
                        time.sleep(10)
                        continue
                    except Exception as re:
                        log.error(f"Token refresh after 403 failed: {re}")

                # After MAX_403_RETRIES, stop hammering — wait 5 minutes
                log.warning(f"403 persists after {consecutive_403s} attempts. Waiting {FORBIDDEN_WAIT//60} minutes...")
                if not error_notified:
                    send_discord(
                        f"⚠️ **403 errors persist** after token refresh.\nWaiting 5 minutes before retrying.",
                        color=0xFEE75C,
                    )
                    error_notified = True
                    in_error_state = True
                time.sleep(FORBIDDEN_WAIT)
                # Reset 403 counter after waiting
                consecutive_403s = 0
                error_notified = False

            else:
                consecutive_errors += 1
                in_error_state = True
                backoff = min(backoff * 2, MAX_BACKOFF)
                log.error(f"HTTP {status} error (attempt {consecutive_errors}). Backing off {backoff}s.")

                if not error_notified:
                    send_discord(
                        f"⚠️ **HTTP {status} errors.**\nBacking off silently. Will notify when resolved.",
                        color=0xFEE75C,
                    )
                    error_notified = True

                time.sleep(backoff)

        except Exception as e:
            consecutive_errors += 1
            in_error_state = True
            backoff = min(backoff * 2, MAX_BACKOFF)
            log.error(f"Unexpected error (attempt {consecutive_errors}): {e}")

            if not error_notified:
                send_discord(f"⚠️ **Watcher error:** `{e}`\nBacking off silently.", color=0xFEE75C)
                error_notified = True

            time.sleep(backoff)


if __name__ == "__main__":
    run()
