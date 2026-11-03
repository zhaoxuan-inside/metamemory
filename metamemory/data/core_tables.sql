-- MetaMemory 核心层表（PG DDL；"数据模型一次定型"清单，核心层技术选型 附录 A）
-- 核心表随 metamemory 包迁移（ADR-002）；SQLite 档业务语义不变。

CREATE TABLE domains (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  display_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
  -- 域无类型字段（公理 1）：个人域/组织域是成员数与事实，不是类型分支
);

CREATE TABLE memberships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  domain_id uuid NOT NULL REFERENCES domains(id),
  member_ref text NOT NULL,
  role_labels jsonb NOT NULL DEFAULT '[]',   -- 角色即属性（ADR 10），json-logic 规则消费
  status text NOT NULL DEFAULT 'active',
  UNIQUE (domain_id, member_ref)
);

CREATE TABLE agent_connections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  domain_id uuid NOT NULL REFERENCES domains(id),
  agent_ref text NOT NULL,
  visibility_ceiling text NOT NULL DEFAULT 'L2',
  authorization_ref uuid,
  audit_enabled boolean NOT NULL DEFAULT true
);

CREATE TABLE vision_rules (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  domain_id uuid NOT NULL,
  action text NOT NULL,
  rule jsonb NOT NULL,          -- json-logic 规则即数据
  enabled boolean NOT NULL DEFAULT true
);

CREATE TABLE authorization_records (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  domain_id uuid NOT NULL,
  grantee text NOT NULL,
  object_ref text NOT NULL,                -- 条目/主题域/时间窗
  level text NOT NULL CHECK (level IN ('L1','L2','L3','L3D','L4','L5')),
  purpose text NOT NULL,
  allowed_purposes jsonb NOT NULL DEFAULT '[]',  -- 允许目的集：公开面裁决谓词（约束 16）
  expires_at timestamptz,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked','expired')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE visibility_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_id uuid, domain_id uuid NOT NULL,
  from_level text, to_level text NOT NULL,
  confirm_ref text, created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memory_episodes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL, domain_id uuid NOT NULL,
  source_uri text NOT NULL,
  content text NOT NULL,
  actor text NOT NULL,
  occurred_at timestamptz NOT NULL,
  topics jsonb NOT NULL DEFAULT '[]',
  content_hash text NOT NULL,
  segment_param_version text,             -- 切分参数版本（可逆重组，约束 13）
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, source_uri)
);
CREATE INDEX idx_episodes_fts ON memory_episodes USING gin (to_tsvector('simple', content));

CREATE TABLE segments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  episode_id uuid NOT NULL REFERENCES memory_episodes(id),
  span int4range NOT NULL,
  text text NOT NULL,
  basis jsonb NOT NULL,                   -- 边界依据（统计信号可归因）
  param_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memory_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL, domain_id uuid NOT NULL,
  subject_id uuid, predicate text NOT NULL, object_value text NOT NULL,
  entry_type text NOT NULL CHECK (entry_type IN ('preference','fact','promise','event','intent','pattern')),
  valid_from timestamptz NOT NULL, valid_to timestamptz,
  learned_at timestamptz NOT NULL DEFAULT now(),
  supersedes uuid REFERENCES memory_entries(id),
  anchor_episode uuid REFERENCES memory_episodes(id),              -- 无引文不记忆；唯一例外 entry_type='intent'（声明即来源）
  anchor_span int4range NOT NULL,
  source_level text NOT NULL CHECK (source_level IN ('S1','S2','S3','S4')),
  confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  visibility text NOT NULL CHECK (visibility IN ('L1','L2','L3','L3D','L4','L5')),
  strength numeric(6,3) NOT NULL DEFAULT 1.0,
  last_recall_at timestamptz,             -- 再巩固窗口锚点（约束 11）
  embedding vector(1024), embedding_model text,
  injected_suspect boolean NOT NULL DEFAULT false,
  generic_tags jsonb NOT NULL DEFAULT '{}',
  gray_forgotten boolean NOT NULL DEFAULT false,                   -- 灰色遗忘降权（manage.forget gray）
  stale boolean NOT NULL DEFAULT false,                            -- 血缘回溯标记（recompute 管道）
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (entry_type = 'intent' OR anchor_episode IS NOT NULL)
);
CREATE INDEX idx_entries_superseded ON memory_entries (supersedes) WHERE supersedes IS NOT NULL;
CREATE INDEX idx_entries_visibility ON memory_entries (tenant_id, visibility);
CREATE INDEX idx_entries_active ON memory_entries (tenant_id, subject_id, predicate) WHERE valid_to IS NULL;

CREATE TABLE entities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL, canonical_name text NOT NULL,
  entity_type text, disambiguation jsonb
);
CREATE TABLE entity_aliases (
  entity_id uuid NOT NULL REFERENCES entities(id),
  alias text NOT NULL,
  PRIMARY KEY (entity_id, alias)
);

CREATE TABLE association_edges (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  edge_type text NOT NULL CHECK (edge_type IN
    ('entity','temporal','contextual','supersedes','semantic','causal','derived','conflict')),
  src_id uuid NOT NULL, dst_id uuid NOT NULL,
  weight numeric(4,3), confidence numeric(4,3),
  evidence jsonb NOT NULL,               -- 无依据不建边
  valid_from timestamptz NOT NULL DEFAULT now(), valid_to timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (src_id, dst_id, edge_type)
);
CREATE INDEX idx_edges_src ON association_edges (src_id, edge_type);

CREATE TABLE intent_details (
  entry_id uuid PRIMARY KEY REFERENCES memory_entries(id),
  state text NOT NULL DEFAULT 'candidate'
    CHECK (state IN ('candidate','active','expired','completed')),
  ttl_expires_at timestamptz,
  visibility text NOT NULL DEFAULT 'L1',
  expected_response text NOT NULL DEFAULT 'solution',
  origin text NOT NULL CHECK (origin IN ('declared','agent_candidate'))
);

CREATE TABLE published_items (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entry_id uuid NOT NULL REFERENCES memory_entries(id),
  level text NOT NULL CHECK (level IN ('L4','L5')),
  allowed_purposes jsonb NOT NULL DEFAULT '[]',
  review_status text NOT NULL DEFAULT 'draft' CHECK (review_status IN ('draft','published','withdrawn')),
  published_at timestamptz,
  withdraw_deadline timestamptz          -- 72h 无痕撤回窗（§11.1-②）
);

CREATE TABLE conflict_queue (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  src_entry uuid NOT NULL, dst_entry uuid NOT NULL,
  detected_rule text, verdict text, decided_by text,
  status text NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE feedback_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  domain_id uuid NOT NULL,
  signal_type text NOT NULL,             -- 显式/隐式/系统内 三级信号
  target_ref text NOT NULL, payload jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE evolution_params (
  domain_id uuid NOT NULL,
  param_key text NOT NULL,               -- 切分粒度/前置阈值/派生门/排序权重/λ…
  param_value jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (domain_id, param_key)
);

CREATE TABLE prompts (
  name text NOT NULL, version int NOT NULL,
  content text NOT NULL, eval_baseline jsonb,
  PRIMARY KEY (name, version)
);
