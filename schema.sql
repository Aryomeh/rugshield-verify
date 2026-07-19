-- ═══════════════════════════════════════════════════════════════════════════
-- RugShield bot schema — Neon Postgres
-- Only tables used by wel.py / set.py / api.py. The sniper/trading bot's
-- tables (tracked_wallets, user_settings, etc.) are a SEPARATE database/project
-- and are intentionally not included here.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS greeted_users (
    user_id    BIGINT NOT NULL,
    chat_id    BIGINT NOT NULL,
    greeted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS contract_address (
    chat_id        BIGINT PRIMARY KEY,
    ca             TEXT NOT NULL,
    set_by         BIGINT NOT NULL,
    set_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    buy_bot_paused BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS fud_violations (
    user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    last_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS bot_groups (
    chat_id  BIGINT PRIMARY KEY,
    title    TEXT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS private_groups (
    chat_id                 BIGINT PRIMARY KEY,
    owner_id                BIGINT NOT NULL,
    title                   TEXT NOT NULL,
    custom_slug             TEXT UNIQUE,
    invite_link             TEXT,
    portal_channel_id       BIGINT DEFAULT NULL,
    portal_channel_username TEXT DEFAULT NULL,
    created_at              TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS verification_tokens (
    token       TEXT PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    chat_id     BIGINT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,   -- unix timestamp, matches time.time()
    used        BOOLEAN NOT NULL DEFAULT FALSE,
    consumed_at DOUBLE PRECISION DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS verified_users (
    user_id     BIGINT NOT NULL,
    chat_id     BIGINT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS welcome_settings (
    chat_id         BIGINT PRIMARY KEY,
    media_file_id   TEXT DEFAULT NULL,
    media_type      TEXT DEFAULT NULL,
    welcome_text    TEXT DEFAULT NULL,
    twitter_url     TEXT DEFAULT NULL,
    website_url     TEXT DEFAULT NULL,
    buy_gif_file_id TEXT DEFAULT NULL,
    set_by          BIGINT DEFAULT NULL,
    set_at          TIMESTAMPTZ DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS setup_state (
    user_id               BIGINT PRIMARY KEY,
    step                  TEXT NOT NULL,
    target_group_chat_id  BIGINT DEFAULT NULL,
    updated_at            TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_actions (
    user_id    BIGINT PRIMARY KEY,
    action     TEXT NOT NULL,
    chat_id    BIGINT NOT NULL,
    extra      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_auto_replies (
    id         SERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    keyword    TEXT NOT NULL,
    reply_text TEXT NOT NULL
);


-- New table: replaces context.job_queue.run_once() delayed auto-deletes.
-- A Vercel Cron job sweeps this every minute and deletes any due messages.
CREATE TABLE IF NOT EXISTS pending_deletes (
    id         SERIAL PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    delete_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_deletes_delete_at ON pending_deletes (delete_at);
