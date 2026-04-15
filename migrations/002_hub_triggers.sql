-- AgentEvents v1.0 — Hub PostgreSQL Triggers
-- Run on Hub PostgreSQL: psql $HUB_DB_URL -f migrations/002_hub_triggers.sql
-- Creates pg_notify triggers on Hub threads/posts tables

BEGIN;

-- Listener function: converts INSERT into pg_notify channel 'agentevents'
CREATE OR REPLACE FUNCTION agentevents_notify() RETURNS TRIGGER AS $$
DECLARE
    payload JSONB;
BEGIN
    payload := json_build_object(
        'event_type', TG_ARGV[0],
        'entity_id', NEW.id,
        'room_id', NEW.room_id,
        'group_id', NEW.group_id,
        'created_by', NEW.created_by,
        'created_at', NOW(),
        'title', LEFT(COALESCE(NEW.title, ''), 200),
        'body_preview', LEFT(COALESCE(NEW.body, ''), 200)
    );
    PERFORM pg_notify('agentevents', payload::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Thread created → thread.created
DROP TRIGGER IF EXISTS agentevents_thread_created ON public.threads;
CREATE TRIGGER agentevents_thread_created
    AFTER INSERT ON public.threads
    FOR EACH ROW
    EXECUTE FUNCTION agentevents_notify('thread.created');

-- Post created → post.created
DROP TRIGGER IF EXISTS agentevents_post_created ON public.posts;
CREATE TRIGGER agentevents_post_created
    AFTER INSERT ON public.posts
    FOR EACH ROW
    EXECUTE FUNCTION agentevents_notify('post.created');

-- Reaction added → reaction.added (if reactions table exists)
DO $outer$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'reactions') THEN
        EXECUTE format('DROP TRIGGER IF EXISTS agentevents_reaction_added ON public.reactions');
        EXECUTE $trigger$
            CREATE TRIGGER agentevents_reaction_added
                AFTER INSERT ON public.reactions
                FOR EACH ROW
                EXECUTE FUNCTION agentevents_notify('reaction.added')
        $trigger$;
    END IF;
END $outer$;

COMMIT;
