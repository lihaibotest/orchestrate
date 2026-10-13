-- Logical PostgreSQL baseline. Codex should convert this into the project's migration framework.
-- Field sizes/retention/partitioning remain subject to capacity review (TBD-09).

create table workflow_family (
  workflow_family_id text primary key,
  tenant_id text not null,
  task_id text not null,
  cmm_context_id text,
  status text not null default 'ACTIVE',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index idx_workflow_family_tenant_task on workflow_family(tenant_id, task_id);

create table registry_snapshot (
  snapshot_id text primary key,
  workflow_family_id text not null references workflow_family(workflow_family_id),
  snapshot_hash text not null,
  payload_ref text,
  payload_json jsonb,
  created_at timestamptz not null default now(),
  unique(workflow_family_id, snapshot_hash)
);

create table workflow_version (
  workflow_family_id text not null references workflow_family(workflow_family_id),
  version integer not null check(version > 0),
  parent_version integer,
  registry_snapshot_id text not null references registry_snapshot(snapshot_id),
  ir_hash text not null,
  structure_ref text,
  status text not null,
  created_at timestamptz not null default now(),
  primary key(workflow_family_id, version)
);

create table execution (
  execution_id text primary key,
  workflow_family_id text not null,
  workflow_version integer not null,
  tenant_id text not null,
  correlation_id text,
  client_request_id text,
  n8n_execution_id text,
  status text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key(workflow_family_id, workflow_version) references workflow_version(workflow_family_id, version),
  unique(tenant_id, workflow_family_id, workflow_version, client_request_id)
);
create index idx_execution_status on execution(status);

create table node_execution (
  execution_id text not null references execution(execution_id),
  node_id text not null,
  status text not null,
  agent_id text,
  updated_at timestamptz not null default now(),
  primary key(execution_id, node_id)
);

create table node_attempt (
  execution_id text not null,
  node_id text not null,
  attempt_no integer not null check(attempt_no > 0),
  idempotency_key text not null,
  status text not null,
  retry_count integer not null default 0,
  started_at timestamptz,
  ended_at timestamptz,
  error_code text,
  error_signature text,
  primary key(execution_id, node_id, attempt_no),
  unique(idempotency_key),
  foreign key(execution_id, node_id) references node_execution(execution_id, node_id)
);

create table artifact_ref (
  artifact_id text primary key,
  tenant_id text not null,
  workflow_family_id text not null,
  workflow_version integer not null,
  execution_id text not null references execution(execution_id),
  execution_mode text not null check (execution_mode in ('PRODUCTION', 'SELF_TEST')),
  node_id text not null,
  attempt_no integer not null,
  output_name text not null,
  artifact_type text not null,
  media_type text not null,
  schema_ref text,
  uri text not null,
  storage_ref text not null,
  content_hash text not null,
  size_bytes bigint not null check (size_bytes >= 0),
  status text not null check (status in ('CREATED', 'VALIDATING', 'VALIDATED', 'CONSUMED', 'INVALID', 'DELETED')),
  validation_status text not null check (validation_status in ('PENDING', 'PASS', 'FAIL')),
  schema_id text,
  schema_version text,
  quality_status text,
  reusable boolean not null default false,
  input_fingerprint text,
  permission_scope jsonb,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique(tenant_id, artifact_id),
  unique(execution_id, node_id, attempt_no, output_name),
  foreign key(execution_id, node_id, attempt_no) references node_attempt(execution_id, node_id, attempt_no)
);

create table artifact_validation (
  validation_id bigserial primary key,
  tenant_id text not null,
  artifact_id text not null,
  validation_type text not null,
  result text not null check (result in ('PASS', 'FAIL')),
  error_code text,
  failure_reason text,
  created_at timestamptz not null default now(),
  foreign key(tenant_id, artifact_id) references artifact_ref(tenant_id, artifact_id)
);

create table checkpoint (
  checkpoint_id text primary key,
  execution_id text not null references execution(execution_id),
  node_id text not null,
  artifact_refs jsonb not null default '[]'::jsonb,
  input_fingerprint text,
  trusted boolean not null default false,
  created_at timestamptz not null default now()
);

create table self_test_run (
  self_test_id text primary key,
  workflow_family_id text not null,
  workflow_version integer not null,
  attempt_no integer not null,
  level text not null,
  passed boolean not null,
  failure_stage text,
  error_signature text,
  repair_action jsonb,
  evidence jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  foreign key(workflow_family_id, workflow_version) references workflow_version(workflow_family_id, version)
);

create table runtime_event (
  event_id text primary key,
  execution_id text not null references execution(execution_id),
  event_type text not null,
  workflow_version integer not null,
  node_id text,
  agent_id text,
  attempt_no integer,
  trace_id text,
  request_id text,
  error_code text,
  payload jsonb not null default '{}'::jsonb,
  occurred_at timestamptz not null
);
create index idx_runtime_event_execution_time on runtime_event(execution_id, occurred_at);

-- P0-10 Runtime Event / Reconciliation projection. Migration 0009 is authoritative.
alter table runtime_event add column if not exists tenant_id text;
alter table runtime_event add column if not exists source text not null default 'LEGACY';
alter table runtime_event add column if not exists correlation_id text;
alter table runtime_event add column if not exists external_reference text;
alter table runtime_event add column if not exists received_at timestamptz not null default now();
alter table runtime_event add column if not exists payload_hash text;
alter table runtime_event add column if not exists event_version integer not null default 1;
alter table runtime_event add column if not exists processing_status text not null default 'RECEIVED';
alter table runtime_event add column if not exists processing_retry_count integer not null default 0;
alter table runtime_event add column if not exists processing_error_code text;
alter table runtime_event add column if not exists processing_error_message text;

create table if not exists runtime_observation (
  observation_id text primary key,
  event_id text not null references runtime_event(event_id) on delete cascade,
  tenant_id text not null,
  correlation_id text not null,
  execution_id text not null references execution(execution_id),
  node_id text,
  attempt_no integer,
  observed_state text not null,
  source text not null,
  confidence text not null default 'AUTHORITATIVE_SOURCE',
  external_reference text,
  occurred_at timestamptz not null,
  evidence jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique(event_id, observed_state)
);
create index if not exists idx_runtime_observation_execution_time
  on runtime_observation(tenant_id, execution_id, occurred_at, created_at);

create table context_outbox (
  event_id text primary key,
  event_type text not null,
  payload jsonb not null,
  status text not null default 'PENDING',
  attempts integer not null default 0,
  next_attempt_at timestamptz,
  created_at timestamptz not null default now(),
  sent_at timestamptz
);
create index idx_context_outbox_pending on context_outbox(status, next_attempt_at, created_at);

-- P0-11 durable CMM sync facts (canonical deployment shape is migration 0010).
create table if not exists cmm_context_event (
    event_id text primary key,
    event_type text not null,
    tenant_id text not null,
    correlation_id text not null,
    workflow_family_id text not null,
    workflow_version integer not null check (workflow_version > 0),
    execution_id text,
    node_id text,
    attempt_no integer,
    payload jsonb not null default '{}'::jsonb,
    occurred_at timestamptz not null,
    created_at timestamptz not null default now()
);
create table if not exists cmm_outbox (
    event_id text primary key references cmm_context_event(event_id) on delete cascade,
    event_type text not null,
    idempotency_key text not null unique,
    payload jsonb not null,
    tenant_id text not null,
    correlation_id text not null,
    status text not null default 'PENDING',
    attempts integer not null default 0,
    next_attempt_at timestamptz,
    lease_until timestamptz,
    last_error text,
    created_at timestamptz not null default now(),
    sent_at timestamptz
);
create index if not exists idx_cmm_outbox_lag on cmm_outbox(status, created_at);

create table state_transition_history (
  transition_id bigserial primary key,
  aggregate_type text not null,
  aggregate_id text not null,
  from_state text,
  to_state text not null,
  reason text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
