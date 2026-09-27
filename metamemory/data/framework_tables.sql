-- MetaMemory 框架层表（PG DDL，框架详细设计 §二；SQLite 档按降级矩阵替换）
-- 框架表随 metamemory_framework 包迁移（ADR-002）。

CREATE TABLE tenants (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan            text NOT NULL DEFAULT 'free',
  quotas          jsonb NOT NULL DEFAULT '{}',
  dek_key_version int  NOT NULL DEFAULT 1
);

CREATE TABLE tasks (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),
  pipeline          text NOT NULL,
  pipeline_version  int  NOT NULL,             -- 创建时解析并绑定快照（A13）
  priority          smallint NOT NULL DEFAULT 30, -- 10 交互读/20 落袋写/30 治理/40 常规/50 批量
  status            text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','waiting','succeeded','failed','partial','canceled')),
  idempotency_key   text,
  params            jsonb NOT NULL DEFAULT '{}',
  config_snapshot   jsonb NOT NULL,
  deadline_at       timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX idx_tasks_dispatch ON tasks (tenant_id, priority, status) WHERE status IN ('pending','running','waiting');

CREATE TABLE task_steps (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id        uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  node_id        text NOT NULL,
  node_type      text NOT NULL CHECK (node_type IN ('step','fork','loop','switch','wait','subflow')),
  step_name      text,  step_version int,
  status         text NOT NULL DEFAULT 'ready'
                 CHECK (status IN ('ready','running','waiting','succeeded','failed','skipped','abstained')),
  attempts       int NOT NULL DEFAULT 0,
  lease_owner    text, lease_expires_at timestamptz,
  join_policy    text CHECK (join_policy IN ('all','quorum','any')),
  parent_ref     bigint,
  input_ref      text, output_ref text,
  iteration      int, loop_key text,
  abstain_reason text,                            -- A15：显式弃权原因码（禁空串）
  error          jsonb,
  started_at timestamptz, finished_at timestamptz,
  UNIQUE (task_id, node_id, COALESCE(loop_key,''), COALESCE(iteration::text,''))
);
CREATE INDEX idx_steps_ready ON task_steps (status, lease_expires_at) WHERE status = 'ready';
CREATE INDEX idx_steps_lease ON task_steps (lease_expires_at) WHERE status = 'running';

CREATE TABLE pipelines (
  name text NOT NULL, version int NOT NULL,
  graph jsonb NOT NULL, checksum text NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','deprecated')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);

CREATE TABLE schedules (
  name text PRIMARY KEY, cron text NOT NULL,
  pipeline text NOT NULL, pipeline_version int NOT NULL,
  tenant_scope text NOT NULL DEFAULT '*',
  leader_lock_holder text, leader_lock_expires_at timestamptz
);

CREATE TABLE data_contracts (
  name text NOT NULL, version int NOT NULL,
  input_map jsonb NOT NULL, flatten_map jsonb NOT NULL,
  assertions jsonb NOT NULL DEFAULT '[]', owner_step text,
  UNIQUE (name, version)
);

CREATE TABLE component_registry (
  name text NOT NULL, version int NOT NULL,
  container jsonb NOT NULL, error_policy jsonb NOT NULL, scheduling jsonb,
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','deprecated')),
  UNIQUE (name, version)
);

CREATE TABLE llm_calls (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id uuid, node_id text NOT NULL, step text NOT NULL,
  input_hash text NOT NULL, prompt_version text NOT NULL,
  provider text NOT NULL, model text,
  response jsonb, cost_tokens int DEFAULT 0, cost_money numeric(12,6) DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (node_id, input_hash, prompt_version)       -- 幂等重放键（A7）
);

CREATE TABLE audit_log (
  id bigint GENERATED ALWAYS AS IDENTITY,
  logged_at timestamptz NOT NULL DEFAULT now(),
  tenant_id uuid, who text NOT NULL, action text NOT NULL,
  object_ref text NOT NULL, authorization_ref text,
  prev_hash text,                                     -- 链式防篡改（审计自身治理）
  PRIMARY KEY (id, logged_at)
) PARTITION BY RANGE (logged_at);

CREATE TABLE metering_events (
  id bigint GENERATED ALWAYS AS IDENTITY,
  tenant_id uuid NOT NULL,
  dimension text NOT NULL,          -- 检索/token/存储/提取条数/容量/发现位
  quantity numeric(18,4) NOT NULL,
  source text, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

CREATE TABLE pipeline_review_queue (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid NOT NULL, task_id uuid, node_id text,
  reason_code text NOT NULL CHECK (reason_code <> '' AND reason_code <> 'unknown'),
  payload jsonb, retry_state text NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now()
);
