# admin.py

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from db import get_pool

logger = logging.getLogger(__name__)

SUPER_ADMIN_ID = 6112522068


def _is_super_admin(user_id: int) -> bool:
    return user_id == SUPER_ADMIN_ID


# ── Stats queries ─────────────────────────────────────────────────────────────

async def _get_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_groups = await conn.fetchval("SELECT COUNT(*) FROM bot_groups")
        total_private_groups = await conn.fetchval("SELECT COUNT(*) FROM private_groups")
        total_verified = await conn.fetchval("SELECT COUNT(*) FROM verified_users")
        total_greeted = await conn.fetchval("SELECT COUNT(DISTINCT user_id) FROM greeted_users")
        groups = await conn.fetch(
            "SELECT title, added_at FROM bot_groups ORDER BY added_at DESC"
        )

    return {
        "total_groups": total_groups,
        "total_private_groups": total_private_groups,
        "total_verified": total_verified,
        "total_greeted": total_greeted,
        "groups": groups,  # list of asyncpg Records: r["title"], r["added_at"]
    }


# ── Panel builder ─────────────────────────────────────────────────────────────

async def _home_text_and_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    stats = await _get_stats()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    text = (
        "👑 *RugShield Super Admin Panel*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏘️ *Groups using bot:* `{stats['total_groups']}`\n"
        f"🔒 *Private groups set up:* `{stats['total_private_groups']}`\n"
        f"✅ *Total verified users:* `{stats['total_verified']}`\n"
        f"👋 *Total greeted users:* `{stats['total_greeted']}`\n\n"
        f"🕐 _{now}_"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 View All Groups", callback_data="adm|groups")],
        [InlineKeyboardButton("🔄 Refresh",         callback_data="adm|home")],
        [InlineKeyboardButton("❌ Close",            callback_data="adm|close")],
    ])

    return text, keyboard


# ── /admin command ────────────────────────────────────────────────────────────

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if chat.type != "private":
        await update.message.reply_text("⚠️ Use /admin in a private DM with the bot.")  # type: ignore
        return

    if not _is_super_admin(user.id):
        await update.message.reply_text("❌ You don't have permission to use this command.")  # type: ignore
        return

    text, keyboard = await _home_text_and_keyboard()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)  # type: ignore


# ── Callback handler ──────────────────────────────────────────────────────────

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    await query.answer()

    if not _is_super_admin(user.id):
        await query.answer("❌ Access denied.", show_alert=True)
        return

    action = query.data.split("|")[1]  # type: ignore

    if action == "home":
        text, keyboard = await _home_text_and_keyboard()
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif action == "groups":
        stats = await _get_stats()
        groups = stats["groups"]

        if not groups:
            body = "_No groups found._"
        else:
            lines = []
            for i, row in enumerate(groups, 1):
                title = row["title"]
                added_at = row["added_at"]
                # added_at is a real TIMESTAMPTZ now, not a text column, so we
                # format it directly instead of the old added_at[:10] string
                # slice (which assumed a SQLite ISO-text timestamp).
                date = added_at.strftime("%Y-%m-%d") if added_at else "?"
                lines.append(f"{i}. *{title}* — _{date}_")
            body = "\n".join(lines)

        text = (
            f"🏘️ *All Groups ({len(groups)})*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{body}"
        )

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="adm|home")]
            ]),
        )

    elif action == "close":
        await query.edit_message_text("👑 Admin panel closed.")