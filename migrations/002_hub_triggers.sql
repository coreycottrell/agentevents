-- AgentEvents v1.0 — Hub PostgreSQL Triggers (FIXED)
-- Run on Hub PostgreSQL: psql $HUB_DB_URL -f migrations/002_hub_triggers.sql
--
-- IMPORTANT: Each table has DIFFERENT columns. The trigger functions
-- must reference only columns that exist on that specific table.
-- threads: id, room_id, title, body, created_by, created_at
-- posts: id, thread_id, body, created_by, created_at
-- reactions: id, target_id, actor_id, emoji, created_at

BEGIN;

-- Thread trigger: has room_id, title, body
CREATE OR REPLACE FUNCTION agentevents_thread_notify() RETURNS TRIGGER AS $$
DECLARE
    grp_id UUID;
BEGIN
    -- Get group_id by joining through rooms table
    SELECT r.group_id INTO grp_id FROM public.rooms r WHERE r.id = NEW.room_id;

    PERFORM pg_notify('agentevents', json_build_object(
        'event_type', 'thread.created',
        'entity_id', NEW.id,
        'room_id', NEW.room_id,
        'group_id', grp_id,
        'created_by', NEW.created_by,
        'created_at', NOW(),
        'title', LEFT(COALESCE(NEW.title, ''), 200),
        'body_preview', LEFT(COALESCE(NEW.body, ''), 200)
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Post trigger: has thread_id, body (no room_id, no title)
CREATE OR REPLACE FUNCTION agentevents_post_notify() RETURNS TRIGGER AS $$
DECLARE
    rm_id UUID;
    grp_id UUID;
    thread_title TEXT;
BEGIN
    -- Get room_id and title from the thread
    SELECT t.room_id, t.title INTO rm_id, thread_title
    FROM public.threads t WHERE t.id = NEW.thread_id;

    -- Get group_id from the room
    SELECT r.group_id INTO grp_id FROM public.rooms r WHERE r.id = rm_id;

    PERFORM pg_notify('agentevents', json_build_object(
        'event_type', 'post.created',
        'entity_id', NEW.id,
        'room_id', rm_id,
        'group_id', grp_id,
        'created_by', NEW.created_by,
        'created_at', NOW(),
        'title', LEFT(COALESCE(thread_title, ''), 200),
        'body_preview', LEFT(COALESCE(NEW.body, ''), 200)
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Thread trigger
DROP TRIGGER IF EXISTS agentevents_thread_created ON public.threads;
CREATE TRIGGER agentevents_thread_created
    AFTER INSERT ON public.threads
    FOR EACH ROW
    EXECUTE FUNCTION agentevents_thread_notify();

-- Post trigger
DROP TRIGGER IF EXISTS agentevents_post_created ON public.posts;
CREATE TRIGGER agentevents_post_created
    AFTER INSERT ON public.posts
    FOR EACH ROW
    EXECUTE FUNCTION agentevents_post_notify();

-- Reaction trigger (only if table exists)
DO $outer$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'reactions') THEN
        EXECUTE 'DROP TRIGGER IF EXISTS agentevents_reaction_added ON public.reactions';
        -- Reactions have: target_id, actor_id, emoji — minimal payload
        EXECUTE $trigger$
            CREATE OR REPLACE FUNCTION agentevents_reaction_notify() RETURNS TRIGGER AS $fn$
            BEGIN
                PERFORM pg_notify('agentevents', json_build_object(
                    'event_type', 'reaction.added',
                    'entity_id', NEW.id,
                    'room_id', NULL,
                    'group_id', NULL,
                    'created_by', NEW.actor_id::text,
                    'created_at', NOW(),
                    'title', NEW.emoji,
                    'body_preview', ''
                )::text);
                RETURN NEW;
            END;
            $fn$ LANGUAGE plpgsql
        $trigger$;
        EXECUTE $trigger2$
            CREATE TRIGGER agentevents_reaction_added
                AFTER INSERT ON public.reactions
                FOR EACH ROW
                EXECUTE FUNCTION agentevents_reaction_notify()
        $trigger2$;
    END IF;
END $outer$;

COMMIT;
