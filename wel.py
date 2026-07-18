# wel.py
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest


# ── DB helpers (all DB code lives in db.py) ───────────────────────────────────
from db import (
    get_social_links,
    init_wel_db,
    # greeted / verified
    has_been_greeted, mark_greeted,
    is_verified, mark_verified,
    # contract address
    get_ca, set_ca, delete_ca, is_buy_bot_paused, set_buy_bot_paused,
    # fud
    add_fud_violation, reset_violations,
    get_fud_words, add_fud_word, remove_fud_word,
    # groups
    register_group, remove_group, get_groups,
    # welcome settings (new)
    get_welcome_settings, set_welcome_media, set_welcome_text,
    remove_welcome_media, set_buy_gif, remove_buy_gif,
    unmark_greeted,
    unmark_verified,
    # auto replies
    get_auto_replies, add_auto_reply, delete_auto_reply,
    # portal channel
    update_group_portal, get_private_group,
    get_private_group_by_channel_id,
    # pending state (replaces in-memory PENDING/VERIFY_PENDING/SETUP_STATE)
    set_pending_action, get_pending_action, pop_pending_action, clear_pending_action,
    queue_verification, set_verification_dm_msg, get_verification, cancel_verification,
    get_setup_state, clear_setup_state,
    # replaces asyncio.sleep()-based fire-and-forget auto-deletes
    queue_message_deletion,
)
from admin import cmd_admin, admin_callback
# ── Private group verification system ────────────────────────────────────────
from set import (
    BOT_USERNAME,
    _start_router,
    check_group_owner,
    cmd_setupgroup,
    handle_webapp_data,
    setupgroup_callback,
    setupgroup_text,
)

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION  — edit this block only
# ══════════════════════════════════════════════════════════════════════════════

import os
BOT_TOKEN             = os.environ["BOT_TOKEN"]
DELETE_AFTER_SECONDS  = 30
VERIFY_TIMEOUT_SEC    = 60
# NOTE: DB_PATH (the old SQLite file path) is gone — all state now lives in
# Neon Postgres via db.py / NEON_DATABASE_URL.

# Super-admins — always have full access
SUPER_ADMINS: set[int] = {
    6112522068,
}

# Welcome text character limit
WELCOME_TEXT_LIMIT = 900

# Auto-reply limits
AUTO_REPLY_MAX_SLOTS    = 5
AUTO_REPLY_TEXT_LIMIT   = 500   # max chars for the reply body
AUTO_REPLY_KEYWORD_LIMIT = 30   # max chars for the trigger keyword

# ── Anti-FUD word list ────────────────────────────────────────────────────────
# NOTE: this list is no longer read/mutated at runtime — it's kept only as a
# reference of the defaults, which now live in the `fud_words` Postgres table
# (seeded once via schema.sql). All runtime checks go through get_fud_words()/
# add_fud_word()/remove_fud_word() in db.py.
FUD_WORDS: list[str] = [
    "scam", "rug", "rugpull", "rug pull", "honeypot", "exit scam",
    "soft rug", "hard rug", "fake project", "fake dev", "dev dumped",
    "team dumped", "dead coin", "project is dead", "coin is dead",
    "this is dead", "dead chart", "going to zero", "0 soon", "zero incoming",
    "sell now", "everyone sell", "dump it", "massive dump", "dumping hard",
    "jeets", "avoid this", "stay away", "not safe",
    "unsafe project", "don't buy", "dont buy", "do not buy", "buyer beware",
     "lp removed", "liquidity gone", "no liquidity", "can't sell",
    "cant sell", "trading disabled", "shitcoin", "trash project",
    "garbage coin", "worthless", "slow rug", "farming exit liquidity",
    "it's over", "game over", "all holders cooked", "we got rugged",
    "rip holders",
]

MUTE_AFTER_VIOLATIONS = 3
MUTE_DURATION_MINUTES = 10

# ── Buy-bot config ────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("aiohttp.client").setLevel(logging.WARNING)

# NOTE: PENDING, VERIFY_PENDING, and SETUP_STATE used to be in-memory dicts
# here. They're now all backed by Postgres (pending_actions,
# pending_verifications, setup_state tables via db.py) — required because
# each Telegram update on Vercel is a separate, memory-less invocation.



# ══════════════════════════════════════════════════════════════════════════════
#  PERMISSION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    if user.id in SUPER_ADMINS:
        return True
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return False
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        return any(a.user.id == user.id for a in admins)
    except Exception:
        return False


async def check_group_admin(bot, chat_id: int, user_id: int) -> bool:
    if user_id in SUPER_ADMINS:
        return True
    try:
        admins = await bot.get_chat_administrators(chat_id)
        return any(a.user.id == user_id for a in admins)
    except Exception:
        return False

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITY
# ══════════════════════════════════════════════════════════════════════════════

async def contains_fud(text: str) -> str | None:
    text_lower = text.lower()
    words = await get_fud_words()
    for word in words:
        if re.search(r'\b' + re.escape(word) + r'\b', text_lower):
            return word
    return None


async def auto_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                      message_id: int, delay: int = 5) -> None:
    """
    Used to be asyncio.sleep(delay) + delete, fired via asyncio.create_task()
    (fire-and-forget). On Vercel the process can be frozen/killed the moment
    the webhook response is sent, so a background sleep of 5-30s isn't
    reliable — and awaiting it in-request would blow Hobby's 10s function
    timeout for anything >= 10s. Instead this queues the delete in Postgres;
    the cron sweep (api/cron/sweep-deletes.py) does the actual deletion.
    Call sites should now `await` this directly (it returns almost
    instantly) instead of wrapping it in asyncio.create_task().
    """
    await queue_message_deletion(chat_id, message_id, delay)

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN PANEL  (DM only)
# ══════════════════════════════════════════════════════════════════════════════

async def _panel_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    ca = await get_ca(chat_id) or "Not set"
    ca_short = (ca[:14] + "…") if len(ca) > 16 else ca
    paused = await is_buy_bot_paused(chat_id)
    bot_status_btn = (
        InlineKeyboardButton("▶️ Start Buy Bot", callback_data=f"p|togglebuybot|{chat_id}")
        if paused else
        InlineKeyboardButton("⏸️ Pause Buy Bot", callback_data=f"p|togglebuybot|{chat_id}")
    )
    ws = await get_welcome_settings(chat_id)
    media_label = "🖼 Set Welcome Image/GIF" if not (ws and ws.get("media_file_id")) else "🔄 Change Welcome Image/GIF"
    buy_gif_label = "🎬 Set Buy Alert GIF" if not (ws and ws.get("buy_gif_file_id")) else "🔄 Change Buy Alert GIF"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📋 CA: {ca_short}", callback_data=f"p|viewca|{chat_id}")],
        [InlineKeyboardButton("✏️ Set CA",            callback_data=f"p|setca|{chat_id}"),
         InlineKeyboardButton("🗑️ Delete CA",         callback_data=f"p|deleteca|{chat_id}")],
        [bot_status_btn],
        [InlineKeyboardButton("➕ Add FUD word",   callback_data=f"p|addfud|{chat_id}"),
         InlineKeyboardButton("➖ Remove FUD word", callback_data=f"p|removefud|{chat_id}")],
        [InlineKeyboardButton("📝 List FUD words",    callback_data=f"p|listfud|{chat_id}")],
        [InlineKeyboardButton("🔄 Reset user strikes", callback_data=f"p|resetstrikes|{chat_id}")],
        # ── Welcome message controls ──────────────────────────────────────────
        [InlineKeyboardButton("💬 Add Welcome Message", callback_data=f"p|addwelcome|{chat_id}")],
        [InlineKeyboardButton(media_label,              callback_data=f"p|setwelcomemedia|{chat_id}"),
         InlineKeyboardButton("🗑 Remove Image/GIF",    callback_data=f"p|removewelcomemedia|{chat_id}")],
        # ── Buy alert GIF controls ────────────────────────────────────────────
        [InlineKeyboardButton(buy_gif_label,            callback_data=f"p|setbuygif|{chat_id}"),
         InlineKeyboardButton("🗑 Remove Buy GIF",      callback_data=f"p|removebuygif|{chat_id}")],
        [InlineKeyboardButton("🔗 Set Social Links", callback_data=f"p|setsocials|{chat_id}")],
        [InlineKeyboardButton("🤖 Auto Replies",     callback_data=f"p|autoreplies|{chat_id}")],
        [InlineKeyboardButton("🔗 Link Portal Channel", callback_data=f"sg|link_portal|{chat_id}")],
        [InlineKeyboardButton("🔙 Back to groups",      callback_data="p|groups|0")],
    ])


def _back_btn(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back to Panel", callback_data=f"p|select|{chat_id}")]
    ])


async def _auto_replies_keyboard(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build the Auto Replies sub-menu text and keyboard for a given chat."""
    replies = await get_auto_replies(chat_id)
    slot_count = len(replies)

    lines = ["🤖 *Auto Replies*\n"]
    if replies:
        lines.append(f"_{slot_count}/{AUTO_REPLY_MAX_SLOTS} slots used_\n")
    else:
        lines.append("_No auto-replies configured yet._\n")

    buttons: list[list[InlineKeyboardButton]] = []

    for r in replies:
        kw_display = r["keyword"]
        # Show a truncated preview of the reply text inline
        preview = r["reply_text"][:40] + ("…" if len(r["reply_text"]) > 40 else "")
        lines.append(f"• *{kw_display}* → _{preview}_")
        buttons.append([
            InlineKeyboardButton(
                f"❌ Delete [{kw_display}]",
                callback_data=f"p|delar|{chat_id}:{r['id']}",
            )
        ])

    if slot_count < AUTO_REPLY_MAX_SLOTS:
        buttons.append([
            InlineKeyboardButton("➕ Add New Keyword", callback_data=f"p|addar|{chat_id}")
        ])
    else:
        lines.append(f"\n⚠️ *Slots Full ({AUTO_REPLY_MAX_SLOTS}/{AUTO_REPLY_MAX_SLOTS})* — delete one to add more.")

    buttons.append([
        InlineKeyboardButton("🔙 Back to Panel", callback_data=f"p|select|{chat_id}")
    ])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a full help message. Works in DM or group."""
    chat = update.effective_chat
    if not chat:
        return

    text = (
        "🛡️ *RugShield Bot — Help Guide*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        "📌 *COMMANDS*\n"
        "/panel — Open the admin panel _(group owner only, DM only)_\n"
        "/start — Verify yourself as a human to join the group\n"
        "/help — Show this message\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ *ADMIN PANEL BUTTONS*\n"
        "_Access via /panel in DM — group owners only_\n\n"

        "📋 *CA: [address]* — View the current contract address set for your group\n\n"

        "✏️ *Set CA* — Set or update your group's official contract address. "
        "The buy bot will start monitoring it immediately\n\n"

        "🗑️ *Delete CA* — Remove the contract address and stop buy bot alerts\n\n"

        "▶️ *Start Buy Bot* / ⏸️ *Pause Buy Bot* — Toggle buy alert notifications on or off "
        "without deleting the CA\n\n"

        "➕ *Add FUD Word* — Add a custom word or phrase to the FUD blocklist. "
        "Anyone posting it in the group gets a strike\n\n"

        "➖ *Remove FUD Word* — Remove a word from the FUD blocklist\n\n"

        "📝 *List FUD Words* — See all currently blocked FUD words\n\n"

        "🔄 *Reset User Strikes* — Clear all FUD violation strikes for a specific user by their Telegram ID\n\n"

        "💬 *Add Welcome Message* — Set a custom welcome text for new members. "
        "Supports `{first_name}` and `{username}` placeholders. Max 900 chars\n\n"

        "🖼 *Set Welcome Image/GIF* — Upload an image or GIF to show alongside the welcome message\n\n"

        "🗑 *Remove Image/GIF* — Remove the welcome image or GIF (welcome message stays)\n\n"

        "🎬 *Set Buy Alert GIF* — Upload a GIF that plays on every buy alert notification\n\n"

        "🗑 *Remove Buy GIF* — Remove the buy alert GIF (alerts will be text only)\n\n"

        "🔗 *Set Social Links* — Set your official X/Twitter and website links. "
        "These are the only external links allowed in the group\n\n"

        "🤖 *Auto Replies* — Set up to 5 keyword triggers. When a member types a matching word, "
        "the bot auto-replies with your configured response\n\n"

        "🔙 *Back to groups* — Return to the group selection list\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 *AUTO REPLIES SUB-MENU*\n\n"

        "➕ *Add New Keyword* — Set a trigger word (Step 1) then the reply text (Step 2). "
        f"Max {AUTO_REPLY_KEYWORD_LIMIT} chars for keyword, {AUTO_REPLY_TEXT_LIMIT} chars for reply. "
        f"Up to {AUTO_REPLY_MAX_SLOTS} slots per group\n\n"

        "❌ *Delete [Keyword]* — Permanently remove that auto-reply\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛡️ *AUTOMATIC GROUP PROTECTION*\n"
        "_Always active — no setup needed_\n\n"

        "🚫 *Anti-Forward* — Forwarded messages are deleted and the sender is banned instantly\n\n"

        "🔗 *Link Filter* — Any link that isn't your official Twitter or website is deleted. "
        "X/Twitter links get a specific warning pointing to the official link\n\n"

        "📋 *CA Guard* — Any contract address that isn't your official CA is deleted and the sender is banned\n\n"

        "💬 *Anti-FUD* — Messages with FUD words give a strike. "
        f"At {MUTE_AFTER_VIOLATIONS} strikes the user is muted for {MUTE_DURATION_MINUTES} minutes\n\n"

        "✅ *Verification* — New members can be prompted to tap a button to verify they are human\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📈 *BUY BOT*\n\n"

        "Monitors your token's contract address on Ethereum, Base, and BSC in real time. "
        "Posts a buy alert to your group whenever a swap is detected, showing the USD value, "
        "market cap, transaction link, and a BUY NOW button. "
        "Buy size is shown with tier emojis:\n"
        "🐳 $5,000+  |  🐬 $1,000+  |  🦈 $500+  |  🐟 $10+\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "_Powered by RugShield 🛡️_"
    )

    await update.effective_message.reply_text(text, parse_mode="Markdown")  # type: ignore


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    if chat.type != "private":
        msg = await update.message.reply_text("⚙️ Open a DM with me and send /panel there.")  # type: ignore
        await auto_delete(context, chat.id, msg.message_id)
        return

    groups = await get_groups()
    if not groups:
        await update.message.reply_text(  # type: ignore
            "⚠️ My group database was reset.\n\n"
            "Please remove and re-add me to your group, then send /panel again."
        )
        return

    accessible = []
    for g in groups:
        try:
            if await check_group_owner(context.bot, g["chat_id"], user.id):
                accessible.append(g)
        except Exception as e:
            logger.warning(f"Owner check failed for {g['chat_id']}: {e}")

    if not accessible:
        await update.message.reply_text(  # type: ignore
            "⛔ You are not the owner of any group I currently manage."
        )
        return

    if len(accessible) == 1:
        g = accessible[0]
        await update.message.reply_text(  # type: ignore
            f"⚙️ *Admin Panel — {g['title']}*",
            parse_mode="Markdown",
            reply_markup=await _panel_keyboard(g["chat_id"]),
        )
        return

    buttons = [
        [InlineKeyboardButton(g["title"], callback_data=f"p|select|{g['chat_id']}")]
        for g in accessible
    ]
    await update.message.reply_text(  # type: ignore
        "⚙️ *Admin Panel — Select a group:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user  = update.effective_user
    if not query or not user:
        return
    await query.answer()

    parts = (query.data or "").split("|")
    if len(parts) != 3 or parts[0] != "p":
        return
    _, action, chat_id_str = parts

    # For delar the payload is "chat_id:reply_id" — parse chat_id from the first segment
    try:
        chat_id = int(chat_id_str.split(":")[0])
    except (ValueError, IndexError):
        return

    # ── Group list ────────────────────────────────────────────────────────────
    if action == "groups":
        groups = await get_groups()
        accessible = []
        for g in groups:
            try:
                if await check_group_owner(context.bot, g["chat_id"], user.id):
                    accessible.append(g)
            except Exception as e:
                logger.warning(f"Owner check failed for {g['chat_id']}: {e}")
        if not accessible:
            await query.edit_message_text("⛔ No owned groups found.")
            return
        buttons = [
            [InlineKeyboardButton(g["title"], callback_data=f"p|select|{g['chat_id']}")]
            for g in accessible
        ]
        await query.edit_message_text(
            "⚙️ *Admin Panel — Select a group:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ── Select group ──────────────────────────────────────────────────────────
    if action == "select":
        if not await check_group_owner(context.bot, chat_id, user.id):
            await query.edit_message_text("⛔ You are not the owner of this group.")
            return
        groups = await get_groups()
        title = next((g["title"] for g in groups if g["chat_id"] == chat_id), str(chat_id))
        await query.edit_message_text(
            f"⚙️ *Admin Panel — {title}*",
            parse_mode="Markdown",
            reply_markup=await _panel_keyboard(chat_id),
        )
        return

    # All actions below require group ownership
    if not await check_group_owner(context.bot, chat_id, user.id):
        await query.edit_message_text("⛔ You are not the owner of this group.")
        return

    # ── Delete auto-reply (special: payload is chat_id:reply_id) ─────────────
    if action == "delar":
        # chat_id_str here is "chat_id:reply_id"
        try:
            cid_str, rid_str = chat_id_str.split(":")
            real_chat_id = int(cid_str)
            reply_id = int(rid_str)
        except (ValueError, AttributeError):
            await query.answer("Invalid request.", show_alert=True)
            return
        await delete_auto_reply(reply_id, real_chat_id)
        text, kb = await _auto_replies_keyboard(real_chat_id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return

    # ── View CA ───────────────────────────────────────────────────────────────
    if action == "viewca":
        ca = await get_ca(chat_id) or "Not set yet."
        await query.edit_message_text(
            f"📋 *Contract Address*\n\n`{ca}`",
            parse_mode="Markdown",
            reply_markup=_back_btn(chat_id),
        )

    # ── Set CA ────────────────────────────────────────────────────────────────
    elif action == "setca":
        await set_pending_action(user.id, {"action": "setca", "chat_id": chat_id})
        await query.edit_message_text(
            "✏️ *Set Contract Address*\n\nSend me the new CA now:",
            parse_mode="Markdown",
        )

    # ── Delete CA ─────────────────────────────────────────────────────────────
    elif action == "deleteca":
        if not await get_ca(chat_id):
            await query.edit_message_text("ℹ️ No CA is set for this group.", reply_markup=_back_btn(chat_id))
            return
        await delete_ca(chat_id)
        await query.edit_message_text(
            "🗑️ *Contract address deleted.*\n\nBuy bot alerts stopped. Use *Set CA* to re-enable.",
            parse_mode="Markdown",
            reply_markup=_back_btn(chat_id),
        )

    # ── Toggle buy bot ────────────────────────────────────────────────────────
    elif action == "togglebuybot":
        if not await get_ca(chat_id):
            await query.edit_message_text("ℹ️ No CA is set — set a CA first.", reply_markup=_back_btn(chat_id))
            return
        paused = await is_buy_bot_paused(chat_id)
        await set_buy_bot_paused(chat_id, not paused)
        if paused:
            await query.edit_message_text(
                "▶️ *Buy bot resumed!*\n\nBuy alerts are now active.",
                parse_mode="Markdown", reply_markup=_back_btn(chat_id),
            )
        else:
            await query.edit_message_text(
                "⏸️ *Buy bot paused.*\n\nNo alerts until resumed.",
                parse_mode="Markdown", reply_markup=_back_btn(chat_id),
            )

    # ── Add FUD ───────────────────────────────────────────────────────────────
    elif action == "addfud":
        await set_pending_action(user.id, {"action": "addfud", "chat_id": chat_id})
        await query.edit_message_text(
            "➕ *Add FUD Word*\n\nSend the word or phrase to block:",
            parse_mode="Markdown",
        )

    # ── Remove FUD ────────────────────────────────────────────────────────────
    elif action == "removefud":
        await set_pending_action(user.id, {"action": "removefud", "chat_id": chat_id})
        await query.edit_message_text(
            "➖ *Remove FUD Word*\n\nSend the word or phrase to unblock:",
            parse_mode="Markdown",
        )

    # ── List FUD ──────────────────────────────────────────────────────────────
    elif action == "listfud":
        fud_word_list = await get_fud_words()
        words = "\n".join(f"• {w}" for w in fud_word_list)
        text  = f"📋 *FUD list ({len(fud_word_list)} words):*\n\n{words}"
        if len(text) > 4000:
            text = text[:3990] + "\n…"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=_back_btn(chat_id))

    # ── Reset strikes ─────────────────────────────────────────────────────────
    elif action == "resetstrikes":
        await set_pending_action(user.id, {"action": "resetstrikes", "chat_id": chat_id})
        await query.edit_message_text(
            "🔄 *Reset User Strikes*\n\nSend the user's *Telegram ID*:",
            parse_mode="Markdown",
        )

    # ── Add welcome message (text) ────────────────────────────────────────────
    elif action == "addwelcome":
        ws = await get_welcome_settings(chat_id)
        current = (ws or {}).get("welcome_text") or ""
        preview = f"\n\n_Current:_ `{current[:80]}{'…' if len(current) > 80 else ''}`" if current else ""
        await set_pending_action(user.id, {"action": "setwelcometext", "chat_id": chat_id})
        await query.edit_message_text(
            f"💬 *Set Welcome Message*{preview}\n\n"
            f"Send your new welcome text (max {WELCOME_TEXT_LIMIT} chars).\n\n"
            "You can use `{first_name}` and `{username}` as placeholders.",
            parse_mode="Markdown",
        )

    # ── Set welcome image/GIF ─────────────────────────────────────────────────
    elif action == "setwelcomemedia":
        await set_pending_action(user.id, {"action": "setwelcomemedia", "chat_id": chat_id})
        await query.edit_message_text(
            "🖼 *Set Welcome Image / GIF*\n\n"
            "Send me the image or GIF you want shown when someone joins.\n\n"
            "_Or type /skip to keep the current one._",
            parse_mode="Markdown",
        )

    # ── Remove welcome image/GIF ──────────────────────────────────────────────
    elif action == "removewelcomemedia":
        ws = await get_welcome_settings(chat_id)
        if not ws or not ws.get("media_file_id"):
            await query.edit_message_text(
                "ℹ️ No welcome image/GIF is set for this group.",
                reply_markup=_back_btn(chat_id),
            )
            return
        await remove_welcome_media(chat_id)
        await query.edit_message_text(
            "🗑 *Welcome image/GIF removed.*\n\n"
            "Future welcome messages will be text-only.",
            parse_mode="Markdown",
            reply_markup=_back_btn(chat_id),
        )

    # ── Set buy alert GIF ─────────────────────────────────────────────────────
    elif action == "setbuygif":
        await set_pending_action(user.id, {"action": "setbuygif", "chat_id": chat_id})
        ws = await get_welcome_settings(chat_id)
        current = ws.get("buy_gif_file_id") if ws else None
        note = "\n\n_A GIF is already set — send a new one to replace it._" if current else ""
        await query.edit_message_text(
            f"🎬 *Set Buy Alert GIF*{note}\n\n"
            "Send me the GIF or animation to show on every buy alert.\n\n"
            "_Send /skip to keep the current one._",
            parse_mode="Markdown",
        )

    elif action == "setsocials":
        await set_pending_action(user.id, {"action": "setsocials", "chat_id": chat_id})
        await query.edit_message_text(
            "🔗 *Set Social Links*\n\n"
            "Send your links like this:\n"
            "`https://x.com/yourtoken`\n"
            "`https://yourwebsite.com`\n\n"
            "_Send only the X link if you don't have a website._",
            parse_mode="Markdown",
        )    

    # ── Remove buy alert GIF ──────────────────────────────────────────────────
    elif action == "removebuygif":
        ws = await get_welcome_settings(chat_id)
        if not ws or not ws.get("buy_gif_file_id"):
            await query.edit_message_text(
                "ℹ️ No buy alert GIF is set for this group.",
                reply_markup=_back_btn(chat_id),
            )
            return
        await remove_buy_gif(chat_id)
        await query.edit_message_text(
            "🗑 *Buy alert GIF removed.*\n\nBuy alerts will be text-only until you set a new one.",
            parse_mode="Markdown",
            reply_markup=_back_btn(chat_id),
        )

    # ── Auto Replies sub-menu ─────────────────────────────────────────────────
    elif action == "autoreplies":
        text, kb = await _auto_replies_keyboard(chat_id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    # ── Add auto-reply — step 1: ask for keyword ──────────────────────────────
    elif action == "addar":
        replies = await get_auto_replies(chat_id)
        if len(replies) >= AUTO_REPLY_MAX_SLOTS:
            text, kb = await _auto_replies_keyboard(chat_id)
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
            return
        await set_pending_action(user.id, {"action": "ar_keyword", "chat_id": chat_id})
        await query.edit_message_text(
            "🤖 *Add Auto Reply — Step 1/2*\n\n"
            f"Send the *trigger keyword* (max {AUTO_REPLY_KEYWORD_LIMIT} chars).\n\n"
            "_Example:_ `CA` or `contract`\n\n"
            "_The bot will reply whenever a message contains this word (case-insensitive)._",
            parse_mode="Markdown",
        )


# ══════════════════════════════════════════════════════════════════════════════
#  DM INPUT HANDLER  (panel follow-ups)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_dm_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg  = update.message
    if not user or not chat or not msg or chat.type != "private":
        return

    # Handle /skip while waiting for welcome media or buy gif
    if msg.text and msg.text.strip() == "/skip":
        pending = await pop_pending_action(user.id)
        if pending and pending["action"] in ("setwelcomemedia", "setbuygif"):
            label = "welcome image/GIF" if pending["action"] == "setwelcomemedia" else "buy alert GIF"
            await msg.reply_text(
                f"✅ Skipped — {label} unchanged.",
                reply_markup=_back_btn(pending["chat_id"]),
            )
        return

    pending = await get_pending_action(user.id)
    if not pending:
        return

    action  = pending["action"]
    chat_id = pending["chat_id"]

    # ── Welcome media / buy gif: waiting for a photo/animation, not text ─────
    if action in ("setwelcomemedia", "setbuygif"):
        # Text received but we wanted media — re-prompt
        if not msg.animation and not msg.photo:
            label = "image or GIF" if action == "setwelcomemedia" else "GIF or animation"
            await msg.reply_text(
                f"⚠️ Please send a *{label}* (not text).\n\n"
                "Send /skip to keep the current one.",
                parse_mode="Markdown",
            )
            return

        await clear_pending_action(user.id)

        if msg.animation:
            file_id    = msg.animation.file_id
            media_type = "animation"
        else:
            file_id    = msg.photo[-1].file_id   # highest-res
            media_type = "photo"

        await set_welcome_media(chat_id, file_id, media_type, user.id)
        await msg.reply_text(
            f"✅ Welcome {'GIF' if media_type == 'animation' else 'image'} saved!\n\n"
            f"`{file_id}`",
            parse_mode="Markdown",
            reply_markup=_back_btn(chat_id),
        )
        return

    # All remaining actions need text
    text = (msg.text or "").strip()
    if not text:
        await msg.reply_text("⚠️ Nothing received. Action cancelled.")
        await clear_pending_action(user.id)
        return

    await clear_pending_action(user.id)

    if action == "setca":
        await set_ca(chat_id, text, user.id)
        await msg.reply_text(
            f"✅ *Contract address updated!*\n\n`{text}`",
            parse_mode="Markdown",
            reply_markup=_back_btn(chat_id),
        )

    elif action == "setsocials":
        from db import set_social_links
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        twitter = lines[0] if len(lines) >= 1 else None
        website = lines[1] if len(lines) >= 2 else None
        await set_social_links(chat_id, twitter, website)
        parts = []
        if twitter:
            parts.append(f"🐦 X: {twitter}")
        if website:
            parts.append(f"🌐 Website: {website}")
        await msg.reply_text(
            "✅ *Social links saved!*\n\n" + "\n".join(parts),
            parse_mode="Markdown",
            reply_markup=_back_btn(chat_id),
        )

    # ─── ADDED: Handle Twitter/X saving input ───────────────────
    elif action == "settwitter":
        from db import set_twitter_link
        await set_twitter_link(chat_id, text, user.id)
        await msg.reply_text(
            f"✅ *Twitter/X link updated!*\n\n{text}",
            parse_mode="Markdown",
            reply_markup=_back_btn(chat_id),
        )

    # ─── ADDED: Handle Website saving input ─────────────────────
    elif action == "setwebsite":
        from db import set_website_link
        await set_website_link(chat_id, text, user.id)
        await msg.reply_text(
            f"✅ *Website link updated!*\n\n{text}",
            parse_mode="Markdown",
            reply_markup=_back_btn(chat_id),
        )

    elif action == "setwelcometext":
        if len(text) > WELCOME_TEXT_LIMIT:
            await msg.reply_text(
                f"⚠️ Text too long ({len(text)} chars). "
                f"Please keep it under {WELCOME_TEXT_LIMIT} characters and try again."
            )
            await set_pending_action(user.id, pending)   # put back so they can retry
            return
        await set_welcome_text(chat_id, text, user.id)
        await msg.reply_text(
            f"✅ *Welcome message saved!*\n\n{text[:300]}{'…' if len(text) > 300 else ''}",
            parse_mode="Markdown",
            reply_markup=_back_btn(chat_id),
        )

    # ── Auto-reply: step 1 — collect keyword ──────────────────────────────────
    elif action == "ar_keyword":
        if len(text) > AUTO_REPLY_KEYWORD_LIMIT:
            await msg.reply_text(
                f"⚠️ Keyword too long ({len(text)} chars). "
                f"Max {AUTO_REPLY_KEYWORD_LIMIT} characters. Try again:"
            )
            await set_pending_action(user.id, pending)  # put back
            return

        # ── Block reserved social keywords if that social is already set ──────
        kw_lower = text.strip().lower()
        twitter_url, website_url = await get_social_links(chat_id)
        _TWITTER_KEYWORDS = {"twitter", "x"}
        _WEBSITE_KEYWORDS = {"website", "web", "site"}
        if kw_lower in _TWITTER_KEYWORDS and twitter_url:
            await msg.reply_text(
                f"⚠️ *\"{text}\"* is already handled automatically — "
                f"the bot replies with your X/Twitter link whenever someone types that.\n\n"
                "Choose a different keyword:",
                parse_mode="Markdown",
            )
            await set_pending_action(user.id, pending)  # put back
            return
        if kw_lower in _WEBSITE_KEYWORDS and website_url:
            await msg.reply_text(
                f"⚠️ *\"{text}\"* is already handled automatically — "
                f"the bot replies with your website link whenever someone types that.\n\n"
                "Choose a different keyword:",
                parse_mode="Markdown",
            )
            await set_pending_action(user.id, pending)  # put back
            return

        # Advance to step 2
        await set_pending_action(user.id, {"action": "ar_text", "chat_id": chat_id, "keyword": text})
        await msg.reply_text(
            f"🤖 *Add Auto Reply — Step 2/2*\n\n"
            f"Keyword saved: `{text}`\n\n"
            f"Now send the *reply text* the bot should post when triggered "
            f"(max {AUTO_REPLY_TEXT_LIMIT} chars).",
            parse_mode="Markdown",
        )

    # ── Auto-reply: step 2 — collect reply text and save ─────────────────────
    elif action == "ar_text":
        keyword = pending.get("keyword", "")
        if len(text) > AUTO_REPLY_TEXT_LIMIT:
            await msg.reply_text(
                f"⚠️ Reply text too long ({len(text)} chars). "
                f"Max {AUTO_REPLY_TEXT_LIMIT} characters. Try again:"
            )
            await set_pending_action(user.id, pending)  # put back
            return
        saved = await add_auto_reply(chat_id, keyword, text)
        if not saved:
            ar_text_msg, ar_kb = await _auto_replies_keyboard(chat_id)
            await msg.reply_text(
                f"⚠️ *Slot limit reached* ({AUTO_REPLY_MAX_SLOTS}/{AUTO_REPLY_MAX_SLOTS}).\n\n"
                "Delete an existing auto-reply first, then try again.",
                parse_mode="Markdown",
                reply_markup=ar_kb,
            )
        else:
            ar_text_msg, ar_kb = await _auto_replies_keyboard(chat_id)
            await msg.reply_text(
                f"✅ *Auto-reply saved!*\n\n"
                f"Keyword: `{keyword}`\n"
                f"Reply: _{text[:200]}{'…' if len(text) > 200 else ''}_",
                parse_mode="Markdown",
                reply_markup=ar_kb,
            )

    elif action == "addfud":
        word = text.lower()
        added = await add_fud_word(word)
        if added:
            await msg.reply_text(
                f'✅ Added *"{word}"* to FUD list.',
                parse_mode="Markdown", reply_markup=_back_btn(chat_id),
            )
        else:
            await msg.reply_text(
                f'ℹ️ *"{word}"* is already in the FUD list.',
                parse_mode="Markdown", reply_markup=_back_btn(chat_id),
            )

    elif action == "removefud":
        word = text.lower()
        removed = await remove_fud_word(word)
        if removed:
            await msg.reply_text(
                f'✅ Removed *"{word}"* from FUD list.',
                parse_mode="Markdown", reply_markup=_back_btn(chat_id),
            )
        else:
            await msg.reply_text(
                f'⚠️ *"{word}"* not found in FUD list.',
                parse_mode="Markdown", reply_markup=_back_btn(chat_id),
            )

    elif action == "resetstrikes":
        try:
            target_id = int(text)
        except ValueError:
            await set_pending_action(user.id, pending)
            await msg.reply_text("⚠️ That's not a valid user ID (numbers only). Try again:")
            return
        await reset_violations(target_id, chat_id)
        await msg.reply_text(
            f"✅ Strikes reset for user `{target_id}`.",
            parse_mode="Markdown", reply_markup=_back_btn(chat_id),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  WELCOME MEDIA HANDLER  (DM — waiting for photo/animation in panel flow)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_dm_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles images/GIFs sent in DM — either for welcome-media setup or file_id lookup."""
    user = update.effective_user
    chat = update.effective_chat
    msg  = update.message
    if not user or not chat or not msg or chat.type != "private":
        return

    # ── Welcome-media setup flow ──────────────────────────────────────────────
    pending = await get_pending_action(user.id)
    if pending and pending["action"] in ("setwelcomemedia", "setbuygif"):
        await clear_pending_action(user.id)
        chat_id = pending["chat_id"]
        action  = pending["action"]

        if msg.animation:
            file_id, media_type = msg.animation.file_id, "animation"
        elif msg.photo:
            file_id, media_type = msg.photo[-1].file_id, "photo"
        else:
            await msg.reply_text("⚠️ Unsupported media type. Please send an image or GIF.")
            await set_pending_action(user.id, pending)  # put back
            return

        if action == "setwelcomemedia":
            await set_welcome_media(chat_id, file_id, media_type, user.id)
            label = "GIF" if media_type == "animation" else "image"
            await msg.reply_text(
                f"✅ Welcome {label} saved!\n\n"
                f"_Telegram File ID (auto-generated):_\n`{file_id}`",
                parse_mode="Markdown",
                reply_markup=_back_btn(chat_id),
            )
        else:  # setbuygif
            if media_type != "animation":
                await msg.reply_text(
                    "⚠️ Buy alert GIF must be an *animation/GIF*, not a static image.\n\n"
                    "Please send a GIF, or /skip.",
                    parse_mode="Markdown",
                )
                await set_pending_action(user.id, pending)  # put back
                return
            await set_buy_gif(chat_id, file_id, user.id)
            await msg.reply_text(
                f"✅ Buy alert GIF saved!\n\n"
                f"_Telegram File ID (auto-generated):_\n`{file_id}`",
                parse_mode="Markdown",
                reply_markup=_back_btn(chat_id),
            )
        return

    # ── Super-admin: get file_id from any uploaded media ─────────────────────
    if user.id not in SUPER_ADMINS:
        return
    if msg.animation:
        await msg.reply_text(f"🎞 *GIF FILE_ID:*\n\n`{msg.animation.file_id}`", parse_mode="Markdown")
    elif msg.video:
        await msg.reply_text(f"🎥 *VIDEO FILE_ID:*\n\n`{msg.video.file_id}`", parse_mode="Markdown")
    elif msg.photo:
        await msg.reply_text(f"🖼 *PHOTO FILE_ID:*\n\n`{msg.photo[-1].file_id}`", parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
#  GROUP TRACKING
# ══════════════════════════════════════════════════════════════════════════════

async def track_bot_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.my_chat_member
    if not result:
        return
    chat       = result.chat
    new_status = result.new_chat_member.status
    if chat.type in ("group", "supergroup"):
        if new_status in ("member", "administrator"):
            await register_group(chat.id, chat.title or str(chat.id))
            logger.info(f"Bot added to: {chat.title} ({chat.id})")
        elif new_status in ("left", "kicked", "banned", "restricted"):
            await remove_group(chat.id)
            logger.info(f"Bot removed from: {chat.title} ({chat.id})")

# ══════════════════════════════════════════════════════════════════════════════
#  GROUP HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def _restrict_user(bot, chat_id: int, user_id: int) -> None:
    await bot.restrict_chat_member(
        chat_id=chat_id, user_id=user_id,
        permissions=ChatPermissions(
            can_send_messages=False, can_send_polls=False,
            can_send_other_messages=False, can_add_web_page_previews=False,
            can_change_info=False, can_invite_users=False, can_pin_messages=False,
        ),
    )


async def _unrestrict_user(bot, chat_id: int, user_id: int) -> None:
    await bot.restrict_chat_member(
        chat_id=chat_id, user_id=user_id,
        permissions=ChatPermissions(
            can_send_messages=True, can_send_polls=True,
            can_send_other_messages=True, can_add_web_page_previews=True,
            can_change_info=False, can_invite_users=True, can_pin_messages=False,
        ),
    )


async def _build_caption(user, chat_id: int, ws: dict | None) -> str:
    """Build the welcome caption — use per-group text if set, else default."""
    first_name = user.first_name or "Friend"
    username   = f"@{user.username}" if user.username else first_name

    if ws and ws.get("welcome_text"):
        try:
            return ws["welcome_text"].format(
                first_name=first_name,
                username=username
            )
        except Exception:
            return ws["welcome_text"]

    twitter_url, website_url = await get_social_links(chat_id)

    lines = [
        f"👋 Welcome, {first_name}!\n\n"
        f"Hey {username}, great to have you in the group 🚀\n\n"
    ]

    if twitter_url:
        lines.append(f"🐦 X: {twitter_url}")

    if website_url:
        lines.append(f"🌐 Website: {website_url}")

    return "\n".join(lines)

async def _send_welcome(bot, chat_id: int, user) -> None:
    """Post the welcome message (with optional per-group media) then auto-delete."""
    ws      = await get_welcome_settings(chat_id)
    caption = await _build_caption(user, chat_id, ws)
    await mark_greeted(user.id, chat_id)

    file_id    = ws.get("media_file_id") if ws else None
    media_type = ws.get("media_type") if ws else None

    if file_id and media_type == "animation":
        media = await bot.send_animation(
            chat_id=chat_id,
            animation=file_id,
            caption=caption
        )
    elif file_id and media_type == "photo":
        media = await bot.send_photo(
            chat_id=chat_id,
            photo=file_id,
            caption=caption
        )
    else:
        # text-only welcome
        media = await bot.send_message(
            chat_id=chat_id,
            text=caption
        )

    # Was asyncio.sleep(DELETE_AFTER_SECONDS) + delete — see auto_delete() above.
    await queue_message_deletion(chat_id, media.message_id, DELETE_AFTER_SECONDS)


# NOTE: _kick_unverified() used to run via loop.call_later() — an in-process
# timer that fires VERIFY_TIMEOUT_SEC after the user joins. That requires the
# process to stay alive that whole time, which a serverless function does
# not. The equivalent logic (ban+unban to kick, then edit the DM to say
# "time's up") now lives in api/cron/sweep-deletes.py, driven by
# db.get_due_kicks() against the pending_verifications table.


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.chat_member:
        return

    result     = update.chat_member
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    user       = result.new_chat_member.user
    chat       = result.chat

    # User left or was kicked — reset so they re-verify on rejoin
    if new_status in ("left", "kicked", "banned"):
        await unmark_verified(user.id, chat.id)
        await unmark_greeted(user.id, chat.id)
        return

    if old_status not in ("left", "kicked") or new_status not in ("member", "restricted"):
        return

    if user.is_bot:
        return

    # ── Portal channel guard ──────────────────────────────────────────────────
    # If this join event is for the public portal/gate channel (not the private
    # group), do nothing — the pinned verify message is already there.
    pg = await get_private_group_by_channel_id(chat.id)
    if pg is not None:
        return  # This chat IS a portal channel — stay silent, pinned msg handles it.

    if await is_verified(user.id, chat.id):
        if not await has_been_greeted(user.id, chat.id):
            await _send_welcome(context.bot, chat.id, user)
        return

    logger.info(f"New unverified member: {user.full_name} ({user.id}) in {chat.title}")

    restrict_ok = True
    try:
        await _restrict_user(context.bot, chat.id, user.id)
    except Exception as e:
        restrict_ok = False
        logger.warning(f"Could not restrict {user.id}: {e}")

    verify_payload = f"verify_{user.id}_{chat.id}"
    deep_link_url  = f"https://t.me/{BOT_USERNAME}?start={verify_payload}"
    group_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔐 Click here to verify", url=deep_link_url)
    ]])

    notice_msg_id = None
    try:
        notice = await context.bot.send_message(
            chat_id=chat.id,
            text=(
                f"👋 Welcome {user.mention_html()}!\n\n"
                f"⚠️ Please verify you are human by clicking the button below.\n"
                f"You have {VERIFY_TIMEOUT_SEC} seconds or you will be removed."
            ),
            parse_mode="HTML",
            reply_markup=group_keyboard,
        )
        notice_msg_id = notice.message_id
        await _delayed_delete(context.bot, chat.id, notice.message_id, 5)
    except Exception as e:
        logger.warning(f"Could not post group notice: {e}")

    # Persist the pending verification — the cron sweep (api/cron/sweep-deletes.py)
    # kicks the user if this row is still here VERIFY_TIMEOUT_SEC from now.
    await queue_verification(user.id, chat.id, notice_msg_id, VERIFY_TIMEOUT_SEC)


async def _delayed_delete(bot, chat_id: int, message_id: int, delay: int) -> None:
    """See auto_delete() above — same fix, same reasoning."""
    await queue_message_deletion(chat_id, message_id, delay)


async def cmd_verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg  = update.message
    if not user or not chat or not msg or chat.type != "private":
        return

    args = context.args
    if not args or not args[0].startswith("verify_"):
        await msg.reply_text("👋 I'm the group verification bot. Join the group to get started!")
        return

    parts = args[0].split("_")
    if len(parts) != 3:
        return
    try:
        target_user_id = int(parts[1])
        target_chat_id = int(parts[2])
    except ValueError:
        return

    if user.id != target_user_id:
        await msg.reply_text("⚠️ This verification link is not for your account.")
        return

    pending_verification = await get_verification(user.id)
    if pending_verification is None:
        if await is_verified(user.id, target_chat_id):
            await msg.reply_text("✅ You're already verified! Go back to the group and start chatting.")
        else:
            await msg.reply_text("⚠️ Your verification session has expired. Please rejoin the group to get a new link.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ I'm not a bot — verify me!", callback_data=f"doverify:{user.id}:{target_chat_id}")
    ]])
    dm = await msg.reply_text(
        f"👋 *One last step!*\n\n"
        "Press the button below to confirm you're a real person and unlock the group.\n\n"
        f"⏳ You have *{VERIFY_TIMEOUT_SEC} seconds* from when you joined.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    await set_verification_dm_msg(user.id, dm.message_id)


async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    if not data.startswith("doverify:"):
        return
    parts = data.split(":")
    if len(parts) != 3:
        return
    try:
        target_id = int(parts[1])
        chat_id   = int(parts[2])
    except ValueError:
        return

    presser = query.from_user.id
    if presser != target_id:
        await query.answer("This button is not for you.", show_alert=True)
        return

    entry = await cancel_verification(presser)
    if entry is None:
        await query.edit_message_text("✅ You're already verified! Go back and enjoy the group.")
        return

    notice_msg_id = entry.get("notice_msg_id")
    if notice_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=notice_msg_id)
        except Exception:
            pass

    await mark_verified(presser, chat_id)
    try:
        await _unrestrict_user(context.bot, chat_id, presser)
    except Exception as e:
        logger.warning(f"Could not unrestrict {presser}: {e}")

    await query.edit_message_text(
        "✅ *Verified!*\n\nYou can now send messages in the group. Welcome! 🚀",
        parse_mode="Markdown",
    )
    logger.info(f"User {presser} verified in chat {chat_id}")

    try:
        if not await has_been_greeted(query.from_user.id, chat_id):
            await _send_welcome(context.bot, chat_id, query.from_user)
    except Exception as e:
        logger.error(f"Welcome send failed for {presser} in {chat_id}: {e}", exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
#  CA / TWITTER AUTO-REPLY
# ══════════════════════════════════════════════════════════════════════════════

async def handle_ca_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type == "private":
        return
    if not update.message or not update.message.text:
        return

    text = update.message.text.lower()
    chat_id = update.effective_chat.id

    # ── Custom Auto-Reply trigger (checked first, highest priority) ───────────
    auto_replies = await get_auto_replies(chat_id)
    if auto_replies:
        twitter_url, website_url = await get_social_links(chat_id)
        _TWITTER_KW = {"twitter", "x"}
        _WEBSITE_KW = {"website", "web", "site"}
        for ar in auto_replies:
            keyword = ar["keyword"].strip().lower()
            if not keyword:
                continue
            # Skip if this keyword is already handled natively by the socials system
            if keyword in _TWITTER_KW and twitter_url:
                continue
            if keyword in _WEBSITE_KW and website_url:
                continue
            pattern = r'(?<![a-z0-9])' + re.escape(keyword) + r'(?![a-z0-9])'
            if re.search(pattern, text, re.IGNORECASE):
                await update.message.reply_text(ar["reply_text"])
                return  # one reply per message — first match wins

    socials = await get_social_links(chat_id)

    # ── X / Twitter ──────────────────────────────────────────
    if re.search(r"\btwitter\b|\bx\b", text):
        twitter_url = socials[0] if socials and len(socials) > 0 else None

        if twitter_url:
            msg = await update.message.reply_text(
                f"🐦 *Follow us on X (Twitter)!*\n\n{twitter_url}",
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )

            await auto_delete(context, chat_id, msg.message_id, delay=20)
        return

    # ── Website ──────────────────────────────────────────────
    if re.search(r"\bwebsite\b|\bweb\b|\bsite\b", text):
        website_url = socials[1] if socials and len(socials) > 1 else None
        if website_url:
            msg = await update.message.reply_text(
                f"🌐 *Official Website*\n\n{website_url}",
                parse_mode="Markdown",
                disable_web_page_preview=False,
            )

            await auto_delete(context, chat_id, msg.message_id, delay=20)
        return

    # ── Contract Address ─────────────────────────────────────
    if not (re.search(r"\bca\b", text) or "contract address" in text):
        return

    ca = await get_ca(chat_id)

    if not ca:
        msg = await update.message.reply_text(
            "🚀 Launching soon... keep your eyes on the chat 👀"
        )
    else:
        msg = await update.message.reply_text(
            f"📋 *Contract Address*\n\n`{ca}`\n\n_Always verify from official sources._",
            parse_mode="Markdown",
        )

    await auto_delete(context, chat_id, msg.message_id, delay=20)

# ══════════════════════════════════════════════════════════════════════════════
#  ANTI-FUD
# ══════════════════════════════════════════════════════════════════════════════

async def anti_fud(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or update.effective_chat.type == "private":
        return
    msg = update.message
    if not msg or not msg.text:
        return
    if await is_admin(update, context):
        return

    matched = await contains_fud(msg.text)
    if not matched:
        return

    user = msg.from_user
    if not user:
        return

    chat_id = msg.chat_id
    user_id = user.id
    mention = f"@{user.username}" if user.username else user.first_name

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
    except Exception as e:
        logger.warning(f"Could not delete FUD: {e}")
        return

    violations = await add_fud_violation(user_id, chat_id)

    if violations >= MUTE_AFTER_VIOLATIONS:
        until = datetime.utcnow() + timedelta(minutes=MUTE_DURATION_MINUTES)
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id, user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            await reset_violations(user_id, chat_id)
            warn_text = f"🔇 {mention} muted for {MUTE_DURATION_MINUTES} min.\n_Strike {violations}/{MUTE_AFTER_VIOLATIONS}_"
        except Exception as e:
            logger.warning(f"Could not mute: {e}")
            warn_text = f"⚠️ {mention}, keep it positive! Strike {violations}/{MUTE_AFTER_VIOLATIONS}"
    else:
        warn_text = f"⚠️ {mention}, FUD is not allowed!\nStrike *{violations}/{MUTE_AFTER_VIOLATIONS}*"

    warn_msg = await context.bot.send_message(chat_id=chat_id, text=warn_text, parse_mode="Markdown")
    await auto_delete(context, chat_id, warn_msg.message_id, delay=15)


# ══════════════════════════════════════════════════════════════════════════════
#  ANTI-FORWARD
# ══════════════════════════════════════════════════════════════════════════════

async def anti_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not update.effective_chat:
        return

    if update.effective_chat.type == "private":
        return

    chat_id = msg.chat_id
    user = msg.from_user

    if not user:
        return

    # Admins bypass all checks
    if await is_admin(update, context):
        return

    text = (msg.text or msg.caption or "").strip()

    socials = await get_social_links(chat_id)
    official_ca = await get_ca(chat_id)

    # ==========================================================
    # FORWARDED MESSAGE CHECK
    # ==========================================================
    is_forwarded = (
        getattr(msg, "forward_origin", None) is not None
        or getattr(msg, "forward_from", None) is not None
        or getattr(msg, "forward_from_chat", None) is not None
        or getattr(msg, "forward_sender_name", None) is not None
    )

    if is_forwarded:
        mention = f"@{user.username}" if user.username else user.first_name

        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=msg.message_id
            )
        except Exception as e:
            logger.warning(f"Could not delete forwarded message: {e}")

        try:
            await context.bot.ban_chat_member(
                chat_id=chat_id,
                user_id=user.id
            )

            notice = (
                f"🚫 {mention} has been *banned* for sending a forwarded message.\n"
                "_Forwarding is not allowed in this group._"
            )
        except Exception as e:
            logger.warning(f"Could not ban user {user.id}: {e}")
            notice = f"⚠️ {mention}'s forwarded message was deleted."

        notice_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=notice,
            parse_mode="Markdown"
        )

        await auto_delete(
                context,
                chat_id,
                notice_msg.message_id,
                delay=15
            )
        return

    # ==========================================================
    # LINK MODERATION
    # ==========================================================
    urls = re.findall(r'https?://[^\s]+', text)

    allowed_urls = set()

    twitter_url, website_url = await get_social_links(chat_id)

    if twitter_url:
        allowed_urls.add(twitter_url.strip())

    if website_url:
        allowed_urls.add(website_url.strip())
    for url in urls:
        if url.strip() not in allowed_urls:
            # ── x.com / twitter.com → point to official social link ──────────
            if re.search(r'https?://(www\.)?(x\.com|twitter\.com)', url, re.IGNORECASE):
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
                except Exception:
                    pass
                mention = f"@{user.username}" if user.username else user.first_name
                official_twitter = twitter_url or "Not set yet."
                notice_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🚫 {mention}, external X/Twitter links are not allowed here.\n\n"
                        f"Use the official link: {official_twitter}"
                    ),
                )
                await auto_delete(context, chat_id, notice_msg.message_id, delay=15)
                return

            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=msg.message_id
                )
            except Exception as e:
                logger.warning(f"Could not delete unauthorized link: {e}")
            return

    # ==========================================================
    # CONTRACT ADDRESS MODERATION
    # ==========================================================
    evm_match = re.search(r"\b0x[a-fA-F0-9]{40}\b", text)
    sol_match = re.search(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", text)
    ca_match = evm_match or sol_match

    if ca_match:
        detected_ca = ca_match.group(0)
        mention = f"@{user.username}" if user.username else user.first_name

        # Allow if it matches the official CA
        if official_ca and detected_ca.lower() == official_ca.lower():
            return

        # No CA set OR wrong CA → delete + ban
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
        except Exception as e:
            logger.warning(f"Could not delete CA message: {e}")

        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
            notice = (
                f"🚫 {mention} has been *banned* for posting an unauthorized contract address.\n"
                "_Only the official CA is allowed in this group._"
            )
        except Exception as e:
            logger.warning(f"Could not ban user {user.id}: {e}")
            notice = f"⚠️ {mention}'s unauthorized CA was deleted."

        notice_msg = await context.bot.send_message(
            chat_id=chat_id, text=notice, parse_mode="Markdown"
        )
        await auto_delete(context, chat_id, notice_msg.message_id, delay=15)
        return


# ─────────────────────────────────────────────────────────────
# PORTAL CHANNEL SELECTION HANDLER
# ─────────────────────────────────────────────────────────────

async def handle_portal_channel_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Fired when an admin shares a channel via the KeyboardButtonRequestChat picker
    (request_id == 5005) in a private DM.

    Workflow:
      1. Validate request_id and SETUP_STATE.
      2. Fetch channel metadata; require a public username.
      3. Save portal details to DB.
      4. Post the portal gate message to the channel and pin it.
      5. Confirm to the admin and clean up state / keyboard.
    """
    from telegram import ReplyKeyboardRemove

    user = update.effective_user
    msg  = update.message
    if not user or not msg:
        return

    # ── 1. Validate request_id ────────────────────────────────────────────────
    chat_shared = msg.chat_shared
    if not chat_shared or chat_shared.request_id != 5005:
        return  # Not our picker — ignore

    # ── 1b. Validate SETUP_STATE ──────────────────────────────────────────────
    state = await get_setup_state(user.id)
    if not state or state.get("step") != "AWAITING_PORTAL_CHANNEL":
        await msg.reply_text(
            "⚠️ No active portal-linking session found. "
            "Please tap *Link Portal Channel* from the admin panel again.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    target_group_chat_id: int = state["target_group_chat_id"]
    channel_id: int = chat_shared.chat_id

    # ── 2. Fetch channel metadata & verify it is public ───────────────────────
    # The bot must already be an admin in the channel before we can call get_chat().
    # Telegram fires chat_shared immediately on tap — before the user adds the bot —
    # so we catch the 400/Chat-not-found case explicitly and tell the admin what to do.
    try:
        channel_obj = await context.bot.get_chat(chat_id=channel_id)
    except Exception as exc:
        err_str = str(exc).lower()
        logger.error(f"[portal] Failed to fetch channel {channel_id}: {exc}")

        if "chat not found" in err_str or "400" in err_str:
            await msg.reply_text(
                "❌ *Bot is not in that channel yet.*\n\n"
                "Please do these steps first, then tap *Link Portal Channel* again:\n\n"
                "1️⃣ Open your channel in Telegram\n"
                "2️⃣ Go to *Administrators* → *Add Administrator*\n"
                "3️⃣ Search for *@rugshieldbot* and add it\n"
                "4️⃣ Enable *Post Messages* and *Invite Users* rights\n"
                "5️⃣ Tap Save, then come back here and tap the button again",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove(),
            )
        else:
            await msg.reply_text(
                "❌ Could not reach that channel. Please try again or contact support.",
                reply_markup=ReplyKeyboardRemove(),
            )
        # Clear state so they can restart cleanly from the panel
        await clear_setup_state(user.id)
        return

    channel_username = channel_obj.username  # None if private
    if not channel_username:
        await msg.reply_text(
            "❌ *That channel doesn't have a public username.*\n\n"
            "The portal channel must be *public* with a `t.me/` link so members can find it.\n\n"
            "Please convert the channel to public in its settings and try again.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        await clear_setup_state(user.id)
        return

    channel_title = channel_obj.title or channel_username

    # ── 3. Save to DB ─────────────────────────────────────────────────────────
    await update_group_portal(
        chat_id=target_group_chat_id,
        portal_id=channel_id,
        portal_username=channel_username,
    )

    # ── 4. Build the deep-link — slug is the channel username ─────────────────
    # channel_username is already lowercased and stored as the slug in update_group_portal
    slug = channel_username.lower()
    bot_me = await context.bot.get_me()
    deep_link = f"https://t.me/{bot_me.username}?start=join_{slug}"

    # ── 5. Post & pin the portal gate message ────────────────────────────────
    gate_text = (
        f"💎 Welcome to {channel_title} Official Gate\n\n"
        "👉 Tap the button below to solve the captcha and get your invite link:"
    )
    gate_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔐 Click Here to Verify", url=deep_link)
    ]])

    try:
        gate_msg = await context.bot.send_message(
            chat_id=channel_id,
            text=gate_text,
            reply_markup=gate_markup,
        )
    except Exception as exc:
        logger.error(f"[portal] Failed to post gate message to {channel_id}: {exc}")
        await msg.reply_text(
            "❌ Could not post the gate message to the channel. "
            "Make sure the bot has the *Post Messages* admin right and try again.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        await clear_setup_state(user.id)
        return

    try:
        await context.bot.pin_chat_message(
            chat_id=channel_id,
            message_id=gate_msg.message_id,
            disable_notification=True,
        )
    except Exception as exc:
        logger.warning(f"[portal] Could not pin gate message in {channel_id}: {exc}")

    # ── 6. Success — confirm and clean up ────────────────────────────────────
    await clear_setup_state(user.id)

    await msg.reply_text(
        f"✅ *Portal Channel Linked Successfully!*\n\n"
        f"📢 Channel: @{channel_username}",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )



# ─────────────────────────────────────────────────────────────
# APPLICATION FACTORY — used by api/telegram.py (webhook entrypoint)
# ─────────────────────────────────────────────────────────────
#
# Replaces main()/run_polling(). No post_init/post_shutdown, no job_queue,
# no run_polling — this just registers handlers and returns an Application
# that api/telegram.py initializes once (per warm container) and calls
# process_update() on for every incoming webhook POST. The buy-bot
# supervisor line was already commented out in the polling version and
# belongs to a separate always-on process anyway (it needs to run
# continuously, which serverless can't do) — see the FOMO bot on EC2.

_application: "Application | None" = None


def build_application() -> Application:
    """Construct the Application with all handlers registered. Does not
    initialize or start it — the caller (api/telegram.py) does that."""
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(ChatMemberHandler(
        track_bot_membership,
        ChatMemberHandler.MY_CHAT_MEMBER,
    ))

    app.add_handler(ChatMemberHandler(
        welcome_new_member,
        ChatMemberHandler.ANY_CHAT_MEMBER,
    ))

    app.add_handler(CommandHandler("panel",      cmd_panel))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(CommandHandler("start", _start_router))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CallbackQueryHandler(
        verify_callback,
        pattern=r"^doverify:\d+:\-?\d+$",
    ))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm\|"))

    app.add_handler(CallbackQueryHandler(
        panel_callback,
        pattern=r"^p\|",
    ))

    app.add_handler(CallbackQueryHandler(
        setupgroup_callback,
        pattern=r"^sg\|",
    ))

    # DM text — panel input handler
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_dm_input,
    ), group=1)

    # DM media
    app.add_handler(MessageHandler(
        (filters.ANIMATION | filters.VIDEO | filters.PHOTO) &
        filters.ChatType.PRIVATE,
        handle_dm_media,
    ))

    # Portal channel picker (chat_shared update from KeyboardButtonRequestChat)
    app.add_handler(MessageHandler(
        filters.StatusUpdate.CHAT_SHARED & filters.ChatType.PRIVATE,
        handle_portal_channel_selection,
    ))

    # Group moderation (anti-forward runs first on all group messages)
    app.add_handler(MessageHandler(
        ~filters.ChatType.PRIVATE,
        anti_forward,
    ))

    _group_txt = filters.TEXT & ~filters.COMMAND & ~filters.ChatType.PRIVATE
    app.add_handler(MessageHandler(_group_txt, handle_ca_request), group=1)
    app.add_handler(MessageHandler(_group_txt, anti_fud),          group=2)

    return app


async def get_application() -> Application:
    """Lazily build + initialize the Application once, reused across warm
    serverless invocations (mirrors db.py's connection-pool pattern)."""
    global _application
    if _application is None:
        _application = build_application()
        await _application.initialize()
    return _application
