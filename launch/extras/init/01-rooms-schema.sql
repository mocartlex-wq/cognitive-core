-- Cognitive Core Rooms — schema init.
-- Mounted into postgres container as /docker-entrypoint-initdb.d/01-rooms-schema.sql.
-- Runs once on FIRST start (when data dir is empty). Idempotent — safe to re-run via
-- `docker compose down -v` followed by `make up`.
--
-- Prereqs: pgcrypto + uuid-ossp extensions (provided by pgvector image).

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------------
-- Rooms — virtual collaboration spaces.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.rooms (
    id          uuid        DEFAULT uuid_generate_v4() PRIMARY KEY,
    name        text        NOT NULL,
    description text,
    created_by  text        NOT NULL,
    api_key     text        NOT NULL UNIQUE,
    status      text        DEFAULT 'active',
    metadata    jsonb       DEFAULT '{}'::jsonb,
    created_at  timestamptz DEFAULT now(),
    closed_at   timestamptz
);

CREATE TABLE IF NOT EXISTS public.room_participants (
    room_id      uuid        NOT NULL REFERENCES public.rooms(id) ON DELETE CASCADE,
    agent_id     text        NOT NULL,
    joined_at    timestamptz DEFAULT now(),
    last_seen_at timestamptz DEFAULT now(),
    role         text        DEFAULT 'member',
    platform     text        DEFAULT 'unknown',
    PRIMARY KEY (room_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_rp_agent ON public.room_participants (agent_id);

CREATE TABLE IF NOT EXISTS public.room_messages (
    id         uuid        DEFAULT uuid_generate_v4() PRIMARY KEY,
    room_id    uuid        REFERENCES public.rooms(id) ON DELETE CASCADE,
    from_agent text        NOT NULL,
    text       text        NOT NULL,
    msg_type   text        DEFAULT 'message',
    parent_id  uuid,
    metadata   jsonb       DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rm_room ON public.room_messages (room_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.room_questions (
    id                 uuid        DEFAULT uuid_generate_v4() PRIMARY KEY,
    room_id            uuid        REFERENCES public.rooms(id) ON DELETE CASCADE,
    message_id         uuid        REFERENCES public.room_messages(id),
    asked_by           text        NOT NULL,
    waiting_for        text[]      NOT NULL,
    answered_by        text[]      DEFAULT ARRAY[]::text[],
    answer_message_ids uuid[]      DEFAULT ARRAY[]::uuid[],
    status             text        DEFAULT 'pending',
    timeout_at         timestamptz,
    created_at         timestamptz DEFAULT now(),
    resolved_at        timestamptz
);
CREATE INDEX IF NOT EXISTS idx_rq_status ON public.room_questions (status, room_id);

-- ---------------------------------------------------------------------------
-- Push pipeline: AFTER INSERT trigger fires pg_notify('room_event', ...).
-- The cognitive-pg-to-nats listener republishes to NATS subject room.<id>.events.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.notify_room_message() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('room_event', json_build_object(
        'event',      'message',
        'room_id',    NEW.room_id::text,
        'message_id', NEW.id::text,
        'from_agent', NEW.from_agent,
        'text',       left(NEW.text, 1000),
        'msg_type',   NEW.msg_type,
        'parent_id',  NEW.parent_id::text,
        'created_at', NEW.created_at::text
    )::text);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS room_msg_notify ON public.room_messages;
CREATE TRIGGER room_msg_notify
  AFTER INSERT ON public.room_messages
  FOR EACH ROW EXECUTE FUNCTION public.notify_room_message();

-- API service ships its own L1/L2/L3 schema on first start (CREATE TABLE in
-- app/db/postgres.py + alembic migrations). Don't pre-create those here —
-- column drift between rooms-init and api-init causes startup failure.
