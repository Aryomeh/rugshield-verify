# api/verify.py
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import uvicorn

from db import consume_verification_token, mark_verified
from telegram import Bot
from set import _generate_invite_link

HCAPTCHA_SECRET = os.environ["HCAPTCHA_SECRET"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

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
    res = requests.post(
        "https://hcaptcha.com/siteverify",
        data={"secret": HCAPTCHA_SECRET, "response": body.captcha_token}
    )
    if not res.json().get("success"):
        return {"ok": False, "reason": "Captcha verification failed. Please try again."}

    result = await consume_verification_token(body.token)
    if not result:
        return {"ok": False, "reason": "Session token expired or already used."}

    if str(result["user_id"]) != body.uid:
        return {"ok": False, "reason": "Token does not match your account."}

    raw_chat_id = result.get("chat_id") or int(body.cid)
    chat_id = normalize_chat_id(int(raw_chat_id))
    user_id = int(body.uid)

    print(f"[verify] user_id={user_id} raw_chat_id={raw_chat_id} normalized_chat_id={chat_id}")

    await mark_verified(user_id, chat_id)

    try:
        bot = Bot(token=BOT_TOKEN)
        invite_link = await _generate_invite_link(bot, chat_id)

        await bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 *You're verified!*\n\n"
                "Here's your single-use invite link:\n"
                f"{invite_link}\n\n"
                "⚠️ *This link self-destructs after 1 use — click it now!*"
            ),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        import traceback
        print(f"[verify] invite link error: {repr(e)}\n{traceback.format_exc()}")
        return {"ok": False, "reason": f"Verified but failed to send invite link: {repr(e)}"}

    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
