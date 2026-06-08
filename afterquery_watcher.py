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
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 120))
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

def refresh_auth_token():
    global auth_token
    log.info("Refreshing auth token...")
    r = requests.post(REFRESH_URL, json={
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
    }, timeout=15)
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
        requests.post(DISCORD_WEBHOOK, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        log.error(f"Discord error: {e}")

def check_available():
    r = requests.get(AVAILABLE_URL, headers=get_headers(), timeout=15)
    if r.status_code == 401:
        log.warning("Got 401 — refreshing token and retrying...")
        refresh_auth_token()
        r = requests.get(AVAILABLE_URL, headers=get_headers(), timeout=15)
    r.raise_for_status()
    return r.json()

def claim_task():
    r = requests.post(CLAIM_URL, headers=get_headers(), timeout=15)
    r.raise_for_status()
    return r.json()

def run():
    try:
        refresh_auth_token()
    except Exception as e:
        log.error(f"Failed to refresh token on startup: {e}")
        send_discord("❌ Failed to refresh token on startup. Check REFRESH_TOKEN in .env", color=0xED4245)
        return

    send_discord(f"🚀 **Watcher started!** Checking every {POLL_INTERVAL // 60} minutes.", color=0xFEE75C)

    next_refresh = time.time() + 55 * 60
    consecutive_errors = 0
    error_notified = False
    backoff = POLL_INTERVAL
    MAX_BACKOFF = 15 * 60
    in_error_state = False

    while True:
        try:
            # Proactive token refresh every 55 minutes
            if time.time() >= next_refresh:
                refresh_auth_token()
                next_refresh = time.time() + 55 * 60

            data = check_available()
            count = data.get("availableCount", 0)
            log.info(f"Available tasks: {count}")

            # Recovered from error state
            if in_error_state:
                in_error_state = False
                error_notified = False
                consecutive_errors = 0
                backoff = POLL_INTERVAL
                send_discord("✅ **Connection restored.** Back to monitoring normally.", color=0x57F287)

            if count > 0:
                log.info("Task available! Claiming...")
                result = claim_task()
                task = result.get("task")
                task_id = task.get("id", "unknown") if task else "unknown"
                send_discord(
                    f"✅ **Task claimed!**\nTask ID: `{task_id}`\n[Open AfterQuery](https://experts.afterquery.com/projects/rewrite)",
                    color=0x57F287,
                )
                log.info(f"Claimed task {task_id}. Sleeping 10 minutes.")
                time.sleep(600)
            else:
                time.sleep(POLL_INTERVAL)

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            consecutive_errors += 1
            in_error_state = True

            if status == 403:
                # First try a token refresh — this is what fixes it manually
                log.warning(f"403 Forbidden (attempt {consecutive_errors}) — trying token refresh...")
                try:
                    refresh_auth_token()
                    next_refresh = time.time() + 55 * 60
                    log.info("Token refreshed after 403. Retrying immediately...")
                    time.sleep(10)  # brief pause before retry
                    continue  # go back to top of loop immediately
                except Exception as re:
                    log.error(f"Token refresh after 403 failed: {re}")
                    backoff = min(backoff * 2, MAX_BACKOFF)
                    log.warning(f"Backing off {backoff // 60:.1f} min.")

            else:
                backoff = min(backoff * 2, MAX_BACKOFF)
                log.error(f"HTTP {status} error (attempt {consecutive_errors}). Backing off {backoff // 60:.1f} min.")

            # Only send ONE Discord alert per error streak
            if not error_notified:
                send_discord(
                    f"⚠️ **AfterQuery returning {status} errors.**\n"
                    f"Attempted token refresh. Backing off and retrying silently.\n"
                    f"Will notify when resolved.",
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
                send_discord(
                    f"⚠️ **Watcher error:** `{e}`\nBacking off and retrying silently. Will notify when resolved.",
                    color=0xFEE75C,
                )
                error_notified = True

            time.sleep(backoff)

if __name__ == "__main__":
    run()
