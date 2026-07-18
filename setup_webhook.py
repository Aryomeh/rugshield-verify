# setup_webhook.py
#
# Run this ONCE after deploying to Vercel (and again if the deploy URL or
# secret ever changes). It tells Telegram to start pushing updates to your
# webhook instead of you needing to poll for them.
#
# Usage:
#   BOT_TOKEN=... WEBHOOK_SECRET=... python3 setup_webhook.py https://yourproject.vercel.app

import os
import sys

import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")  # optional but recommended


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 setup_webhook.py https://yourproject.vercel.app")
        sys.exit(1)

    base_url = sys.argv[1].rstrip("/")
    webhook_url = f"{base_url}/api/telegram"

    payload = {
        "url": webhook_url,
        "allowed_updates": [
            "chat_member",
            "my_chat_member",
            "message",
            "callback_query",
            "chat_shared",
        ],
    }
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET

    res = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
        json=payload,
    )
    print(res.json())

    info = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo")
    print(info.json())


if __name__ == "__main__":
    main()