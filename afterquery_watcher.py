import time
import logging
import requests
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 180))
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

def headers():
    return {
        "Authorization": f"Bearer {auth_token}",
        "Content-Type": "application/json",
        "Origin": "https://experts.afterquery.com",
        "Referer": "https://experts.afterquery.com/projects/rewrite",
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
    r = requests.get(AVAILABLE_URL, headers=headers(), timeout=15)
    if r.status_code == 401:
        log.warning("Got 401 — refreshing token and retrying...")
        refresh_auth_token()
        r = requests.get(AVAILABLE_URL, headers=headers(), timeout=15)
    r.raise_for_status()
    return r.json()

def claim_task():
    r = requests.post(CLAIM_URL, headers=headers(), timeout=15)
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

    while True:
        try:
            if time.time() >= next_refresh:
                refresh_auth_token()
                next_refresh = time.time() + 55 * 60

            data = check_available()
            count = data.get("availableCount", 0)
            log.info(f"Available tasks: {count}")
            consecutive_errors = 0

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

        except Exception as e:
            consecutive_errors += 1
            log.error(f"Error ({consecutive_errors}): {e}")
            if consecutive_errors >= 3:
                send_discord(f"❌ 3 consecutive errors: `{e}`", color=0xED4245)
                consecutive_errors = 0
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()