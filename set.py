# set.py — webhook/serverless-safe version
#
# Changes from the original:
#   1. SETUP_STATE (in-memory dict) → Postgres `setup_state` table via db.py.
#      Required because the portal-channel-link flow spans multiple separate
#      Telegram updates (button click, then a later message), which are
#      separate serverless invocations here — nothing survives in memory
#      between them.
#   2. context.job_queue.run_once(...) delayed deletes → db.queue_message_deletion(),
#      swept by api/cron/sweep-deletes.py. job_queue needs a persistent
#      APScheduler loop, which doesn't exist in a function that dies right
#      after responding.
#   3. Hardcoded HCAPTCHA_SECRET + the duplicate verify_captcha() helper are
#      removed — they were dead code here (api.py already owns the real
#      hCaptcha check against a DIFFERENT hcaptcha.com URL). Keeping two
#      divergent copies of a secret-bearing check was a bug waiting to
#      happen; api.py stays the single source of truth.
#   4. VERIFY_WEB_URL now reads from an env var (falls back to the same
#      default) so it isn't hardcoded per-deploy.

from __future__ import annotations

import json
import os
import urllib.parse
from asyncio.log import logger

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import ContextTypes

from db import (
    mark_verified,
    register_private_group,
    get_private_group_by_slug,
    get_private_group_by_channel_username,
    create_verification_token,
    get_groups,
    get_setup_state,
    set_setup_state,
    clear_setup_state,
    queue_message_deletion,
)

# ── Captcha / web verification base URL ──────────────────────────────────────
VERIFY_WEB_URL = os.environ.get("VERIFY_WEB_URL", "https://rugshield-verify.vercel.app")

BOT_USERNAME = "communityshieldbot"


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def check_group_owner(bot, chat_id: int, user_id: int) -> bool:
    """Returns True only if user_id is the group creator."""
    try:
        admins = await bot.get_chat_administrators(chat_id)
        return any(a.user.id == user_id and a.status == "creator" for a in admins)
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  /panel → "Link Portal Channel" callback handler
#  Triggered by callback_data "sg|link_portal|{chat_id}"
# ══════════════════════════════════════════════════════════════════════════════

async def setupgroup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = query.from_user
    data = (query.data or "").split("|")   # ["sg", action, value]
    if len(data) != 3 or data[0] != "sg":
        return

    action = data[1]
    value = data[2]

    # ── Cancel ────────────────────────────────────────────────────────────────
    if action == "cancel":
        await clear_setup_state(user.id)
        await query.edit_message_text("❌ Setup cancelled.")
        return

    # ── Link Portal Channel ───────────────────────────────────────────────────
    if action == "link_portal":
        try:
            chat_id = int(value)
        except ValueError:
            return

        # Confirm ownership
        if not await check_group_owner(context.bot, chat_id, user.id):
            await query.edit_message_text("⛔ You are not the owner of that group.")
            return

        # Check the group is private
        try:
            chat_obj = await context.bot.get_chat(chat_id)
            if chat_obj.username:
                await query.edit_message_text(
                    "⚠️ *Your group is still public.*\n\n"
                    "Please go to group settings and make it *private* first "
                    "(remove the public username), then tap the button again.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ I've made it private", callback_data=f"sg|link_portal|{chat_id}"),
                        InlineKeyboardButton("❌ Cancel", callback_data="sg|cancel|0"),
                    ]]),
                )
                return
        except Exception as e:
            logger.warning(f"Could not fetch chat {chat_id}: {e}")

        # Auto-register the group if not already in private_groups
        # (title fetched from bot_groups; slug will be set later from channel username)
        all_groups = await get_groups()
        title = next((g["title"] for g in all_groups if g["chat_id"] == chat_id), "Group")
        await register_private_group(
            chat_id=chat_id,
            owner_id=user.id,
            title=title,
            custom_slug=None,   # will be set automatically when channel is linked
        )

        # Enter AWAITING_PORTAL_CHANNEL state — persisted in Postgres so the
        # NEXT incoming update (a separate serverless invocation) can read it.
        await set_setup_state(
            user_id=user.id,
            step="AWAITING_PORTAL_CHANNEL",
            target_group_chat_id=chat_id,
        )

        from telegram import (
            KeyboardButton,
            KeyboardButtonRequestChat,
            ReplyKeyboardMarkup,
        )

        channel_picker = ReplyKeyboardMarkup(
            keyboard=[[
                KeyboardButton(
                    text="📢 Select My Channel",
                    request_chat=KeyboardButtonRequestChat(
                        request_id=5005,
                        chat_is_channel=True,
                        chat_is_created=True,
                    ),
                )
            ]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )

        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "🔗 *Link a Public Portal Channel*\n\n"
                "Before tapping the button below, make sure you have:\n\n"
                "1️⃣ Added *@communityshieldbot* as an *Admin* to your channel\n"
                "2️⃣ Given it *Post Messages* and *Invite Users* permissions\n"
                "3️⃣ Made the channel *public* (it must have a t\\.me/ link)\n\n"
                "Once that's done, tap the button to select your channel:"
            ),
            parse_mode="Markdown",
            reply_markup=channel_picker,
        )
        return


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY LINK HANDLER  — /start join_<slug>
# ══════════════════════════════════════════════════════════════════════════════

async def _start_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Unified /start deep-link handler.
      join_<slug> — user coming from portal channel verify button.
                    Opens a Telegram Mini App for hCaptcha verification.
                    The invite link is sent after via handle_webapp_data().
    """
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message
    if not user or not chat or not msg or chat.type != "private":
        return

    args = context.args
    if not args:
        await msg.reply_text(f"👋 Hello {user.first_name}! Use an official group link to get started.")
        return

    start_arg = args[0]

    # ── CASE 1: coming from the portal channel verify button ──────────────────
    if start_arg.startswith("join_"):
        slug = start_arg[len("join_"):]
        pg = await get_private_group_by_slug(slug)

        if not pg:
            pg = await get_private_group_by_channel_username(slug)

        if not pg:
            await msg.reply_text(
                "❌ This invite link is invalid or the group no longer uses this bot.\n"
                "Contact the group admin for a fresh link."
            )
            return

        chat_id = pg["chat_id"]

        # Create a verification token
        token = await create_verification_token(user.id, chat_id)
        safe_title = urllib.parse.quote(pg["title"])

        verify_url = (
            f"{VERIFY_WEB_URL}?token={token}"
            f"&uid={user.id}"
            f"&cid={chat_id}"
            f"&group={safe_title}"
        )

        sent_verification_msg = await msg.reply_text(
            f"👋 *Welcome!* You're trying to join *{pg['title']}*.\n\n"
            "To get in, you need to pass a quick human check.\n\n"
            "⏳ *You have exactly 1 minute to complete verification before this message self-destructs!*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔐 Verify to Join",
                    web_app=WebAppInfo(url=verify_url)  # ← Opens inside Telegram as Mini App
                )
            ]]),
        )

        # Queue auto-deletion for the verification prompt in ~60 seconds
        # (swept by api/cron/sweep-deletes.py — may lag by up to ~60s vs. exact)
        await queue_message_deletion(user.id, sent_verification_msg.message_id, 60)
        return

    # ── Unrecognised argument ─────────────────────────────────────────────────
    await msg.reply_text(f"👋 Hello {user.first_name}! Use an official group link to get started.")


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fires when the Telegram Mini App calls tg.sendData() after successful verification.
    Receives the verified uid + cid, then sends the single-use invite link.
    """
    msg = update.message
    user = update.effective_user
    if not msg or not msg.web_app_data or not user:
        return

    # ── Parse the payload sent from tg.sendData() ─────────────────────────────
    try:
        payload = json.loads(msg.web_app_data.data)
    except (json.JSONDecodeError, TypeError):
        await msg.reply_text("❌ Something went wrong. Please try verifying again.")
        return

    # ── Sanity checks ─────────────────────────────────────────────────────────
    if not payload.get("verified"):
        await msg.reply_text("❌ Verification was not completed. Please try again.")
        return

    # Double-check the uid in the payload matches the actual Telegram sender
    # (tg.sendData is cryptographically tied to the real user, but belt-and-suspenders)
    if str(user.id) != str(payload.get("uid")):
        await msg.reply_text("⚠️ User mismatch. Please restart verification.")
        return

    chat_id = int(payload["cid"])

    # ── Mark user as verified in DB ───────────────────────────────────────────
    await mark_verified(user.id, chat_id)

    # ── Generate single-use invite link and send it ───────────────────────────
    try:
        invite_link = await _generate_invite_link(context.bot, chat_id)

        sent = await msg.reply_text(
            "🎉 *You're verified!*\n\n"
            f"{invite_link}\n\n"
            "⚠️ *This link expires soon—click it now*",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )

        # Auto-delete the invite link message after ~45 seconds (swept, see above)
        await queue_message_deletion(user.id, sent.message_id, 45)

    except Exception as e:
        import traceback
        logger.error(
            f"Invite link failed for chat {chat_id}: {repr(e)}\n"
            f"{traceback.format_exc()}"
        )
        await msg.reply_text(
            f"✅ Verified, but failed to generate invite link.\n\n"
            f"Error: {repr(e)}\n\n"
            "Please contact the group admin."
        )


# ══════════════════════════════════════════════════════════════════════════════
#  STUBS — kept so wel.py imports don't break; they are no longer wired up
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_setupgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /setupgroup is retired. All setup now happens inside /panel → Link Portal Channel.
    This stub keeps existing handler registrations from crashing.
    """
    msg = update.message
    if msg:
        await msg.reply_text(
            "ℹ️ Use /panel to manage your group settings, including linking a portal channel."
        )


async def setupgroup_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stub — slug text input step no longer exists."""
    pass


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _generate_invite_link(bot, chat_id: int) -> str:
    """Generate a single-use invite link for a private group with NO strict expiration time."""
    link = await bot.create_chat_invite_link(
        chat_id=chat_id,
        member_limit=1  # Highly secure: self-destructs the absolute second 1 person clicks it
        # expire_date is intentionally omitted so it never rots instantly
    )
    return link.invite_link


