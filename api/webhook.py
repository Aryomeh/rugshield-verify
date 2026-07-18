# api/telegram.py
#
# Webhook entrypoint. Replaces wel.py's main()/run_polling(). Telegram POSTs
# each update here (after you call setWebhook — see setup_webhook.py); we
# hand it to the same python-telegram-bot Application/handlers from wel.py,
# just via process_update() instead of an internal polling loop.
#
# The Application is built + initialized once per warm container and reused
# across invocations (same pattern as db.py's connection pool) — cold starts
# pay the initialize() cost, warm ones don't.

import os

from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update

from wel import get_application

app = FastAPI()

# Optional shared-secret check — Telegram can be told to send this header on
# every webhook call via the `secret_token` param when you call setWebhook.
# Set WEBHOOK_SECRET as an env var and pass the same value to setWebhook.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")


@app.post("/api/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    application = await get_application()

    body = await request.json()
    update = Update.de_json(body, application.bot)

    # process_update() runs the same handler-matching logic run_polling()
    # used internally — no behavior change for a given update, just a
    # different delivery mechanism (push vs pull).
    await application.process_update(update)

    return {"ok": True}