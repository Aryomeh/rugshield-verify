# db.py — RugShield bot data layer, rewritten for Neon Postgres (asyncpg).


from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone

import asyncpg 

DATABASE_URL = os.environ["NEON_DATABASE_URL"]

TOKEN_TTL = 1800  # seconds — 30 minutes, matches original

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    """
    Lazily create the connection pool and reuse it across warm serverless
    invocations. A fresh pool is only created after a true cold start.
    """
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=0,
            max_size=5,
            statement_cache_size=0,
        )
    return _pool


def _utcnow():
    return datetime.now(timezone.utc)


# ══════════════════════════════════════════════════════════════════════════
#  INIT
# ══════════════════════════════════════════════════════════════════════════

async def init_wel_db() -> None:
    """
    Kept for API-compatibility with the old call site. Table creation now
    lives in schema.sql (run once against Neon via `psql` or Neon's SQL
    editor) rather than executing CREATE TABLE on every cold start — that's
    the Postgres-appropriate pattern and avoids repeated DDL calls under
    serverless concurrency. This function is a no-op you can safely keep
    calling from old code paths.
    """
    return None


# ══════════════════════════════════════════════════════════════════════════
#  PRIVATE GROUPS
# ══════════════════════════════════════════════════════════════════════════

async def register_private_group(
    chat_id: int,
    owner_id: int,
    title: str,
    custom_slug: "str | None" = None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO private_groups (chat_id, owner_id, title, custom_slug)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id) DO UPDATE
            SET owner_id = excluded.owner_id,
                title = excluded.title,
                custom_slug = COALESCE(excluded.custom_slug, private_groups.custom_slug)
            """,
            chat_id, owner_id, title, custom_slug,
        )


async def get_private_group_by_slug(slug: str) -> "dict | None":
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM private_groups WHERE custom_slug=$1", slug
        )
    return dict(row) if row else None


async def get_private_group_by_channel_username(username: str) -> "dict | None":
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM private_groups WHERE portal_channel_username=$1",
            username.lstrip("@"),
        )
    return dict(row) if row else None


async def get_private_group_by_channel_id(channel_id: int) -> "dict | None":
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM private_groups WHERE portal_channel_id=$1", channel_id
        )
    return dict(row) if row else None


async def get_private_group(chat_id: int) -> "dict | None":
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM private_groups WHERE chat_id=$1", chat_id
        )
    return dict(row) if row else None


async def slug_is_taken(slug: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM private_groups WHERE custom_slug=$1", slug
        )
    return row is not None


async def set_private_group_invite(chat_id: int, invite_link: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE private_groups SET invite_link=$1 WHERE chat_id=$2",
            invite_link, chat_id,
        )


async def remove_private_group(chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM private_groups WHERE chat_id=$1", chat_id)


async def update_group_portal(chat_id: int, portal_id: int, portal_username: str) -> None:
    clean_username = portal_username.lstrip("@").lower()
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """
                UPDATE private_groups
                SET portal_channel_id = $1,
                    portal_channel_username = $2,
                    custom_slug = $3
                WHERE chat_id = $4
                """,
                portal_id, clean_username, clean_username, chat_id,
            )
            # "UPDATE 0" means no row existed — insert one so the slug is always set
            if result == "UPDATE 0":
                await conn.execute(
                    """
                    INSERT INTO private_groups
                        (chat_id, owner_id, title, custom_slug, portal_channel_id, portal_channel_username)
                    VALUES ($1, 0, '', $2, $3, $4)
                    ON CONFLICT (chat_id) DO NOTHING
                    """,
                    chat_id, clean_username, portal_id, clean_username,
                )


# ══════════════════════════════════════════════════════════════════════════
#  VERIFICATION TOKENS
# ══════════════════════════════════════════════════════════════════════════

async def create_verification_token(user_id: int, chat_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO verification_tokens (token, user_id, chat_id, used, created_at)
            VALUES ($1, $2, $3, FALSE, $4)
            """,
            token, user_id, chat_id, now,
        )
    return token


async def consume_verification_token(token: str) -> "dict | None":
    """
    Validate and consume a token (called from api.py after hCaptcha passes).
    Returns {user_id, chat_id} on success, None if expired/used/missing.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM verification_tokens WHERE token=$1 AND used=FALSE FOR UPDATE",
                token,
            )
            if not row:
                return None
            row = dict(row)

            if time.time() - row["created_at"] > TOKEN_TTL:
                await conn.execute(
                    "DELETE FROM verification_tokens WHERE token=$1", token
                )
                return None

            await conn.execute(
                "UPDATE verification_tokens SET used=TRUE, consumed_at=$1 WHERE token=$2",
                time.time(), token,
            )

    return {"user_id": row["user_id"], "chat_id": row["chat_id"]}


async def get_verified_token(token: str) -> "dict | None":
    """
    Read a token already marked used=TRUE by api.py. Bot-side redirect window
    starts from consumed_at, not created_at.
    """
    BOT_REDIRECT_TTL = 600  # seconds
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM verification_tokens WHERE token=$1 AND used=TRUE", token
        )
    if not row:
        return None

    consumed_at = row["consumed_at"] or row["created_at"]
    if time.time() - consumed_at > BOT_REDIRECT_TTL:
        return None

    return {"user_id": row["user_id"], "chat_id": row["chat_id"]}


async def cleanup_expired_tokens() -> None:
    """Purge tokens older than TTL. Wire this into the same Vercel Cron
    endpoint that sweeps pending_deletes, or its own scheduled job."""
    cutoff = time.time() - TOKEN_TTL
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM verification_tokens WHERE created_at < $1", cutoff
        )


# ══════════════════════════════════════════════════════════════════════════
#  VERIFIED USERS
# ══════════════════════════════════════════════════════════════════════════

async def is_verified(user_id: int, chat_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM verified_users WHERE user_id=$1 AND chat_id=$2",
            user_id, chat_id,
        )
    return row is not None


async def mark_verified(user_id: int, chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO verified_users (user_id, chat_id, verified_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, chat_id) DO NOTHING
            """,
            user_id, chat_id, _utcnow(),
        )


async def unmark_verified(user_id: int, chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM verified_users WHERE user_id=$1 AND chat_id=$2",
            user_id, chat_id,
        )


# ══════════════════════════════════════════════════════════════════════════
#  BOT GROUPS
# ══════════════════════════════════════════════════════════════════════════

async def register_group(chat_id: int, title: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_groups (chat_id, title, added_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (chat_id) DO UPDATE SET title = excluded.title
            """,
            chat_id, title, _utcnow(),
        )


async def remove_group(chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM bot_groups WHERE chat_id=$1", chat_id)


async def get_groups() -> "list[dict]":
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT chat_id, title FROM bot_groups")
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════
#  WELCOME SETTINGS (media/text) + SOCIAL LINKS + BUY GIF
# ══════════════════════════════════════════════════════════════════════════

async def get_welcome_settings(chat_id: int) -> "dict | None":
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT media_file_id, media_type, welcome_text, buy_gif_file_id
            FROM welcome_settings WHERE chat_id=$1
            """,
            chat_id,
        )
    return dict(row) if row else None


async def set_welcome_media(chat_id: int, file_id: str, media_type: str, set_by: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO welcome_settings (chat_id, media_file_id, media_type, set_by, set_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (chat_id) DO UPDATE
            SET media_file_id = excluded.media_file_id,
                media_type = excluded.media_type,
                set_by = excluded.set_by,
                set_at = excluded.set_at
            """,
            chat_id, file_id, media_type, set_by, _utcnow(),
        )


async def set_welcome_text(chat_id: int, text: str, set_by: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO welcome_settings (chat_id, welcome_text, set_by, set_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id) DO UPDATE
            SET welcome_text = excluded.welcome_text,
                set_by = excluded.set_by,
                set_at = excluded.set_at
            """,
            chat_id, text, set_by, _utcnow(),
        )


async def remove_welcome_media(chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE welcome_settings SET media_file_id=NULL, media_type=NULL WHERE chat_id=$1",
            chat_id,
        )


async def clear_welcome_settings(chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM welcome_settings WHERE chat_id=$1", chat_id)


async def get_buy_gif(chat_id: int) -> "str | None":
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT buy_gif_file_id FROM welcome_settings WHERE chat_id=$1", chat_id
        )
    return row["buy_gif_file_id"] if row else None


async def set_buy_gif(chat_id: int, file_id: str, set_by: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO welcome_settings (chat_id, buy_gif_file_id, set_by, set_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id) DO UPDATE
            SET buy_gif_file_id = excluded.buy_gif_file_id,
                set_by = excluded.set_by,
                set_at = excluded.set_at
            """,
            chat_id, file_id, set_by, _utcnow(),
        )


async def remove_buy_gif(chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE welcome_settings SET buy_gif_file_id=NULL WHERE chat_id=$1", chat_id
        )


async def get_social_links(chat_id: int) -> "tuple[str | None, str | None]":
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT twitter_url, website_url FROM welcome_settings WHERE chat_id=$1",
            chat_id,
        )
    if row:
        return row["twitter_url"], row["website_url"]
    return None, None


async def set_social_links(chat_id: int, twitter: "str | None", website: "str | None") -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO welcome_settings (chat_id, twitter_url, website_url, set_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id) DO UPDATE
            SET twitter_url = excluded.twitter_url,
                website_url = excluded.website_url,
                set_at = excluded.set_at
            """,
            chat_id, twitter, website, _utcnow(),
        )


async def set_twitter_link(chat_id: int, twitter_url: str, set_by: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO welcome_settings (chat_id, twitter_url, set_by, set_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id) DO UPDATE
            SET twitter_url = excluded.twitter_url,
                set_by = excluded.set_by,
                set_at = excluded.set_at
            """,
            chat_id, twitter_url, set_by, _utcnow(),
        )


async def set_website_link(chat_id: int, website_url: str, set_by: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO welcome_settings (chat_id, website_url, set_by, set_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id) DO UPDATE
            SET website_url = excluded.website_url,
                set_by = excluded.set_by,
                set_at = excluded.set_at
            """,
            chat_id, website_url, set_by, _utcnow(),
        )


# ══════════════════════════════════════════════════════════════════════════
#  GREETED USERS
# ══════════════════════════════════════════════════════════════════════════

async def has_been_greeted(user_id: int, chat_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM greeted_users WHERE user_id=$1 AND chat_id=$2",
            user_id, chat_id,
        )
    return row is not None


async def mark_greeted(user_id: int, chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO greeted_users (user_id, chat_id, greeted_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, chat_id) DO NOTHING
            """,
            user_id, chat_id, _utcnow(),
        )


async def unmark_greeted(user_id: int, chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM greeted_users WHERE user_id=$1 AND chat_id=$2",
            user_id, chat_id,
        )


# ══════════════════════════════════════════════════════════════════════════
#  CONTRACT ADDRESS
# ══════════════════════════════════════════════════════════════════════════

async def get_ca(chat_id: int) -> "str | None":
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ca FROM contract_address WHERE chat_id=$1", chat_id
        )
    return row["ca"] if row else None


async def set_ca(chat_id: int, ca: str, set_by: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO contract_address (chat_id, ca, set_by, set_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (chat_id) DO UPDATE
            SET ca = excluded.ca, set_by = excluded.set_by, set_at = excluded.set_at
            """,
            chat_id, ca, set_by, _utcnow(),
        )


async def delete_ca(chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM contract_address WHERE chat_id=$1", chat_id)


async def get_ca_groups() -> "list[int]":
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT chat_id FROM contract_address WHERE buy_bot_paused = FALSE"
        )
    return [row["chat_id"] for row in rows]


async def is_buy_bot_paused(chat_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT buy_bot_paused FROM contract_address WHERE chat_id=$1", chat_id
        )
    return bool(row["buy_bot_paused"]) if row else False


async def set_buy_bot_paused(chat_id: int, paused: bool) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE contract_address SET buy_bot_paused=$1 WHERE chat_id=$2",
            paused, chat_id,
        )


# ══════════════════════════════════════════════════════════════════════════
#  FUD VIOLATIONS
# ══════════════════════════════════════════════════════════════════════════

async def add_fud_violation(user_id: int, chat_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO fud_violations (user_id, chat_id, count, last_at)
                VALUES ($1, $2, 1, $3)
                ON CONFLICT (user_id, chat_id) DO UPDATE
                SET count = fud_violations.count + 1, last_at = excluded.last_at
                """,
                user_id, chat_id, _utcnow(),
            )
            row = await conn.fetchrow(
                "SELECT count FROM fud_violations WHERE user_id=$1 AND chat_id=$2",
                user_id, chat_id,
            )
    return row["count"]


async def reset_violations(user_id: int, chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE fud_violations SET count=0 WHERE user_id=$1 AND chat_id=$2",
            user_id, chat_id,
        )


# ══════════════════════════════════════════════════════════════════════════
#  AUTO REPLIES
# ══════════════════════════════════════════════════════════════════════════

async def get_auto_replies(chat_id: int) -> "list[dict]":
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, keyword, reply_text FROM custom_auto_replies WHERE chat_id=$1 ORDER BY id",
            chat_id,
        )
    return [dict(r) for r in rows]


async def add_auto_reply(chat_id: int, keyword: str, reply_text: str) -> bool:
    """Returns False if the chat already has 7 auto-replies (slot limit)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM custom_auto_replies WHERE chat_id=$1", chat_id
            )
            if count >= 7:
                return False
            await conn.execute(
                "INSERT INTO custom_auto_replies (chat_id, keyword, reply_text) VALUES ($1, $2, $3)",
                chat_id, keyword.strip(), reply_text.strip(),
            )
    return True


async def delete_auto_reply(reply_id: int, chat_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM custom_auto_replies WHERE id=$1 AND chat_id=$2",
            reply_id, chat_id,
        )


# ══════════════════════════════════════════════════════════════════════════
#  SETUP STATE  — replaces the in-memory SETUP_STATE dict in set.py.
#  Needed because the portal-channel-linking flow spans multiple separate
#  incoming Telegram updates, which are separate serverless invocations here.
# ══════════════════════════════════════════════════════════════════════════

async def set_setup_state(user_id: int, step: str, target_group_chat_id: "int | None" = None) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO setup_state (user_id, step, target_group_chat_id, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE
            SET step = excluded.step,
                target_group_chat_id = excluded.target_group_chat_id,
                updated_at = excluded.updated_at
            """,
            user_id, step, target_group_chat_id, _utcnow(),
        )


async def get_setup_state(user_id: int) -> "dict | None":
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT step, target_group_chat_id FROM setup_state WHERE user_id=$1", user_id
        )
    return dict(row) if row else None


async def clear_setup_state(user_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM setup_state WHERE user_id=$1", user_id)


# ══════════════════════════════════════════════════════════════════════════
#  PENDING ACTIONS  — replaces the in-memory PENDING dict in wel.py.
#  `data` must contain "action" and "chat_id"; any other keys (e.g. "keyword"
#  during the 2-step auto-reply flow) are stored in `extra` and re-merged
#  flat on read, so call sites like pending["keyword"] don't need to change.
# ══════════════════════════════════════════════════════════════════════════

async def set_pending_action(user_id: int, data: dict) -> None:
    action = data["action"]
    chat_id = data["chat_id"]
    extra = {k: v for k, v in data.items() if k not in ("action", "chat_id")}
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_actions (user_id, action, chat_id, extra, updated_at)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            ON CONFLICT (user_id) DO UPDATE
            SET action = excluded.action,
                chat_id = excluded.chat_id,
                extra = excluded.extra,
                updated_at = excluded.updated_at
            """,
            user_id, action, chat_id, json.dumps(extra), _utcnow(),
        )


async def get_pending_action(user_id: int) -> "dict | None":
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT action, chat_id, extra FROM pending_actions WHERE user_id=$1", user_id
        )
    if not row:
        return None
    extra = row["extra"] if isinstance(row["extra"], dict) else json.loads(row["extra"] or "{}")
    return {"action": row["action"], "chat_id": row["chat_id"], **extra}


async def pop_pending_action(user_id: int) -> "dict | None":
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT action, chat_id, extra FROM pending_actions WHERE user_id=$1", user_id
            )
            if row:
                await conn.execute("DELETE FROM pending_actions WHERE user_id=$1", user_id)
    if not row:
        return None
    extra = row["extra"] if isinstance(row["extra"], dict) else json.loads(row["extra"] or "{}")
    return {"action": row["action"], "chat_id": row["chat_id"], **extra}


async def clear_pending_action(user_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM pending_actions WHERE user_id=$1", user_id)


# ══════════════════════════════════════════════════════════════════════════
#  PENDING VERIFICATIONS  — replaces VERIFY_PENDING + loop.call_later() in
#  welcome_new_member(). The actual kick now happens in the cron sweep
#  (api/cron/sweep-deletes.py), not an in-process timer.
# ══════════════════════════════════════════════════════════════════════════

async def queue_verification(user_id: int, chat_id: int, notice_msg_id: "int | None", delay_seconds: int) -> None:
    from datetime import timedelta

    kick_at = _utcnow() + timedelta(seconds=delay_seconds)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_verifications (user_id, chat_id, notice_msg_id, dm_msg_id, kick_at)
            VALUES ($1, $2, $3, NULL, $4)
            ON CONFLICT (user_id) DO UPDATE
            SET chat_id = excluded.chat_id,
                notice_msg_id = excluded.notice_msg_id,
                dm_msg_id = NULL,
                kick_at = excluded.kick_at
            """,
            user_id, chat_id, notice_msg_id, kick_at,
        )


async def set_verification_dm_msg(user_id: int, dm_msg_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE pending_verifications SET dm_msg_id=$1 WHERE user_id=$2",
            dm_msg_id, user_id,
        )


async def get_verification(user_id: int) -> "dict | None":
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT chat_id, notice_msg_id, dm_msg_id FROM pending_verifications WHERE user_id=$1",
            user_id,
        )
    return dict(row) if row else None


async def cancel_verification(user_id: int) -> "dict | None":
    """Pop (fetch + delete) a pending verification — called when the user
    successfully verifies in time, so the cron sweep won't kick them."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT chat_id, notice_msg_id, dm_msg_id FROM pending_verifications WHERE user_id=$1",
                user_id,
            )
            if row:
                await conn.execute("DELETE FROM pending_verifications WHERE user_id=$1", user_id)
    return dict(row) if row else None


async def get_due_kicks() -> "list[dict]":
    """Fetch + remove all pending_verifications rows whose timer has expired."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT user_id, chat_id, notice_msg_id, dm_msg_id FROM pending_verifications WHERE kick_at <= now()"
            )
            if rows:
                ids = [r["user_id"] for r in rows]
                await conn.execute(
                    "DELETE FROM pending_verifications WHERE user_id = ANY($1::bigint[])", ids
                )
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════
#  FUD WORDS  — replaces the in-memory FUD_WORDS list mutation in wel.py's
#  addfud/removefud handlers. Seeded once from schema.sql's default list.
# ══════════════════════════════════════════════════════════════════════════

async def get_fud_words() -> "list[str]":
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT word FROM fud_words ORDER BY word")
    return [r["word"] for r in rows]


async def add_fud_word(word: str) -> bool:
    """Returns False if the word was already present."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "INSERT INTO fud_words (word) VALUES ($1) ON CONFLICT (word) DO NOTHING", word
        )
    return result == "INSERT 0 1"


async def remove_fud_word(word: str) -> bool:
    """Returns False if the word wasn't present."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM fud_words WHERE word=$1", word)
    return result == "DELETE 1"


# ══════════════════════════════════════════════════════════════════════════
#  PENDING DELETES  — replaces context.job_queue.run_once() delayed deletes.
#  A Vercel Cron job calls sweep_pending_deletes() every minute.
# ══════════════════════════════════════════════════════════════════════════

async def queue_message_deletion(chat_id: int, message_id: int, delay_seconds: int) -> None:
    """
    Schedule a message for deletion `delay_seconds` from now. Replaces the
    old context.job_queue.run_once(...) pattern — a Vercel Cron sweep picks
    this up (see api/cron/sweep_deletes.py), so actual deletion may lag by
    up to ~1 minute versus the old exact timer, which is fine for this use case.
    """
    from datetime import timedelta

    pool = await get_pool()
    delete_at = _utcnow() + timedelta(seconds=delay_seconds)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pending_deletes (chat_id, message_id, delete_at) VALUES ($1, $2, $3)",
            chat_id, message_id, delete_at,
        )


async def get_due_deletes() -> "list[dict]":
    """Fetch + remove all pending_deletes rows whose time has come."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                "SELECT id, chat_id, message_id FROM pending_deletes WHERE delete_at <= now()"
            )
            if rows:
                ids = [r["id"] for r in rows]
                await conn.execute(
                    "DELETE FROM pending_deletes WHERE id = ANY($1::int[])", ids
                )
    return [dict(r) for r in rows]