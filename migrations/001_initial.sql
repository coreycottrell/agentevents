-- AgentEvents v1.0 — Initial Schema Migration
-- Run on Hub PostgreSQL: psql $HUB_DB_URL -f migrations/001_initial.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS agentevents;

-- Subscriptions registry
CREATE TABLE agentevents.subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    civ_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'global',
    scope_id UUID,
    delivery_method TEXT NOT NULL DEFAULT 'webhook',
    webhook_url TEXT,
    agentmail_address TEXT,
    muted_until TIMESTAMPTZ,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(civ_id, event_type, scope_type, scope_id)
);

CREATE INDEX ix_ae_subs_civ_id ON agentevents.subscriptions(civ_id);
CREATE INDEX ix_ae_subs_event_type ON agentevents.subscriptions(event_type);
CREATE INDEX ix_ae_subs_active ON agentevents.subscriptions(active);
CREATE INDEX ix_ae_subs_muted ON agentevents.subscriptions(muted_until)
    WHERE muted_until IS NOT NULL;

-- Events store
CREATE TABLE agentevents.events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_ae_events_type ON agentevents.events(event_type);
CREATE INDEX ix_ae_events_created ON agentevents.events(created_at);

-- Delivery tracking
CREATE TABLE agentevents.deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL REFERENCES agentevents.events(id),
    subscription_id UUID NOT NULL REFERENCES agentevents.subscriptions(id),
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    error_message TEXT
);

CREATE INDEX ix_ae_deliv_status ON agentevents.deliveries(status)
    WHERE status = 'pending';
CREATE INDEX ix_ae_deliv_event_sub ON agentevents.deliveries(event_id, subscription_id);

COMMIT;
