# api/cron/sweep-deletes.py


import os

import httpx
from fastapi import FastAPI, Header, HTTPException

from db import get_due_deletes

BOT_TOKEN = os.environ["BOT_TOKEN"]
# On Hobby we're not using Vercel's built-in cron (it caps free accounts at
# once/day), so CRON_SECRET is NOT auto-provisioned here. Generate one
# yourself — e.g. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
# — set it as an env var in Vercel, and configure your external scheduler
# (cron-job.org, etc.) to send it as `Authorization: Bearer <value>`. This
# stops randoms from hitting the endpoint and mass-deleting messages.
CRON_SECRET = os.environ.get("CRON_SECRET")

app = FastAPI()


@app.get("/api/cron/sweep-deletes")
async def sweep_deletes(authorization: str = Header(default="")):
    if CRON_SECRET and authorization != f"Bearer {CRON_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    due = await get_due_deletes()
    if not due:
        return {"ok": True, "deleted": 0}

    deleted = 0
    failed = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for row in due:
            try:
                res = await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
                    json={"chat_id": row["chat_id"], "message_id": row["message_id"]},
                )
                if res.json().get("ok"):
                    deleted += 1
                else:
                    # Message may already be gone (user deleted it manually) —
                    # not a real failure, just log and move on.
                    failed += 1
            except Exception:
                failed += 1

    return {"ok": True, "deleted": deleted, "failed": failed}