# api/verify.py
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import uvicorn

from db import consume_verification_token, mark_verified

HCAPTCHA_SECRET = os.environ["HCAPTCHA_SECRET"]

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://rugshield-verify.vercel.app"],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)


class VerifyRequest(BaseModel):
    captcha_token: str
    token: str
    uid: str
    cid: str
    init_data: str = ""


def normalize_chat_id(cid: int) -> int:
    """
    Telegram supergroup IDs must be in the format -100XXXXXXXXX.
    If the stored chat_id is a bare positive number or missing the -100 prefix,
    fix it here so the bot can find the group.
    """
    s = str(cid)
    if cid > 0:
        return int(f"-100{s}")
    if s.startswith("-100"):
        return cid
    if s.startswith("-"):
        return int(f"-100{s[1:]}")
    return cid


@app.post("/api/verify")
async def verify(body: VerifyRequest):
    # 1. Verify hCaptcha
    res = requests.post(
        "https://hcaptcha.com/siteverify",
        data={
            "secret": HCAPTCHA_SECRET,
            "response": body.captcha_token,
        }
    )
    if not res.json().get("success"):
        return {"ok": False, "reason": "Captcha verification failed. Please try again."}

    # 2. Validate + consume session token (single-use)
    result = await consume_verification_token(body.token)
    if not result:
        return {"ok": False, "reason": "Session token expired or already used."}

    if str(result["user_id"]) != body.uid:
        return {"ok": False, "reason": "Token does not match your account."}

    # 3. Resolve chat_id
    raw_chat_id = result.get("chat_id") or int(body.cid)
    chat_id = normalize_chat_id(int(raw_chat_id))
    user_id = int(body.uid)

    print(f"[verify] user_id={user_id} raw_chat_id={raw_chat_id} normalized_chat_id={chat_id}")

    # 4. Mark user as verified in DB
    # Invite-link generation + sending now happens in wel.py's
    # handle_webapp_data(), triggered by the frontend's tg.sendData() call
    # right after this returns ok=true. Keeping it in one place avoids
    # sending two separate invite links for one verification.
    await mark_verified(user_id, chat_id)

    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
