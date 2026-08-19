-- Adds per-group forward-message moderation settings + strike tracking.
-- Run this once in Supabase's SQL Editor.

ALTER TABLE welcome_settings
    ADD COLUMN IF NOT EXISTS forward_delete_enabled BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE welcome_settings
    ADD COLUMN IF NOT EXISTS forward_action TEXT NOT NULL DEFAULT 'kick';
    -- 'kick'  = delete message + remove user immediately (can rejoin via invite link)
    -- 'warn'  = delete message + warn, kick only after 3 strikes

CREATE TABLE IF NOT EXISTS forward_violations (
    user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    last_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, chat_id)
);
