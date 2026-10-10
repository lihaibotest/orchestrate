\connect agent_registry agent_registry

CREATE TABLE IF NOT EXISTS public.registered_agents (
  id BIGSERIAL PRIMARY KEY,
  agent_id TEXT UNIQUE NOT NULL,
  agent_name TEXT NOT NULL,
  agent_type TEXT NOT NULL,
  description TEXT,
  access_token TEXT,
  refresh_token TEXT,
  expires_at TIMESTAMPTZ,
  endpoint TEXT NOT NULL,
  health_url TEXT NOT NULL,
  llm_model TEXT,
  system_prompt TEXT,
  capabilities JSONB NOT NULL DEFAULT '{"list":[]}'::jsonb,
  intents JSONB NOT NULL DEFAULT '[]'::jsonb,
  priority INTEGER NOT NULL DEFAULT 100,
  weight INTEGER NOT NULL DEFAULT 100,
  heartbeat_ttl_seconds INTEGER NOT NULL DEFAULT 300,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  flag INTEGER NOT NULL DEFAULT 0,
  log_path JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_heartbeat TIMESTAMPTZ,
  n8n_workflow_id TEXT,
  failure_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.registered_agents ALTER COLUMN access_token DROP NOT NULL;
ALTER TABLE public.registered_agents ALTER COLUMN refresh_token DROP NOT NULL;
ALTER TABLE public.registered_agents ALTER COLUMN expires_at DROP NOT NULL;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.registered_agents
    WHERE status NOT IN ('active', 'stale', 'disabled', 'pending_verification')
       OR heartbeat_ttl_seconds < 30 OR priority < 0 OR weight < 0
       OR btrim(endpoint) = '' OR btrim(health_url) = ''
       OR jsonb_typeof(capabilities) <> 'object'
       OR jsonb_typeof(intents) <> 'array'
       OR jsonb_typeof(metadata) <> 'object'
       OR jsonb_typeof(log_path) <> 'object'
  ) THEN
    RAISE EXCEPTION 'registered_agents contains rows that violate release constraints';
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'registered_agents_status_check') THEN
    ALTER TABLE public.registered_agents ADD CONSTRAINT registered_agents_status_check
      CHECK (status IN ('active', 'stale', 'disabled', 'pending_verification'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'registered_agents_ttl_check') THEN
    ALTER TABLE public.registered_agents ADD CONSTRAINT registered_agents_ttl_check
      CHECK (heartbeat_ttl_seconds >= 30);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'registered_agents_priority_check') THEN
    ALTER TABLE public.registered_agents ADD CONSTRAINT registered_agents_priority_check
      CHECK (priority >= 0);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'registered_agents_weight_check') THEN
    ALTER TABLE public.registered_agents ADD CONSTRAINT registered_agents_weight_check
      CHECK (weight >= 0);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'registered_agents_endpoint_check') THEN
    ALTER TABLE public.registered_agents ADD CONSTRAINT registered_agents_endpoint_check
      CHECK (btrim(endpoint) <> '' AND btrim(health_url) <> '');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'registered_agents_capabilities_shape_check') THEN
    ALTER TABLE public.registered_agents ADD CONSTRAINT registered_agents_capabilities_shape_check
      CHECK (jsonb_typeof(capabilities) = 'object' AND jsonb_typeof(capabilities->'list') = 'array');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'registered_agents_intents_shape_check') THEN
    ALTER TABLE public.registered_agents ADD CONSTRAINT registered_agents_intents_shape_check
      CHECK (jsonb_typeof(intents) = 'array');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'registered_agents_metadata_shape_check') THEN
    ALTER TABLE public.registered_agents ADD CONSTRAINT registered_agents_metadata_shape_check
      CHECK (jsonb_typeof(metadata) = 'object');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'registered_agents_log_path_shape_check') THEN
    ALTER TABLE public.registered_agents ADD CONSTRAINT registered_agents_log_path_shape_check
      CHECK (jsonb_typeof(log_path) = 'object');
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS registered_agents_routing_idx
  ON public.registered_agents (status, priority, weight DESC, last_heartbeat DESC);
CREATE INDEX IF NOT EXISTS registered_agents_capabilities_gin_idx
  ON public.registered_agents USING GIN (capabilities);

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
