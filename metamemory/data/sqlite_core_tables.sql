-- MetaMemory SQLite 档 schema·核心层（lite 档真源，ADR-002 框架/核心表分离迁移）。
-- 与 PG DDL（core_tables.sql）表名与语义对齐（类型映射同上）。
-- 由 metamemory.domain.persist.PersistentMemoryStore 装载（幂等）。

-- ===== 核心层 =====

CREATE TABLE IF NOT EXISTS memory_episodes (
  episode_id   TEXT PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  domain_id    TEXT NOT NULL,
  source_uri   TEXT NOT NULL,
  content      TEXT NOT NULL,
  actor        TEXT NOT NULL,
  occurred_at  TEXT NOT NULL,
  topics       TEXT NOT NULL DEFAULT '[]',
  content_hash TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  UNIQUE (tenant_id, source_uri)
);
CREATE INDEX IF NOT EXISTS idx_episodes_hash ON memory_episodes (content_hash);

CREATE TABLE IF NOT EXISTS segments (
  segment_id    TEXT PRIMARY KEY,
  episode_id    TEXT NOT NULL REFERENCES memory_episodes(episode_id) ON DELETE CASCADE,
  text          TEXT NOT NULL,
  span_start    INTEGER NOT NULL,
  span_end      INTEGER NOT NULL,
  basis         TEXT NOT NULL,
  param_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_episode ON segments (episode_id);

CREATE TABLE IF NOT EXISTS memory_entries (
  entry_id         TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  domain_id        TEXT NOT NULL,
  subject          TEXT NOT NULL DEFAULT '',
  predicate        TEXT NOT NULL DEFAULT '',
  object_value     TEXT NOT NULL,
  entry_type       TEXT NOT NULL CHECK (entry_type IN ('preference','fact','promise','event','intent','pattern')),
  valid_from       TEXT NOT NULL,
  valid_to         TEXT,
  learned_at       REAL NOT NULL,
  supersedes       TEXT REFERENCES memory_entries(entry_id),
  anchor_episode   TEXT NOT NULL,
  anchor_start     INTEGER NOT NULL DEFAULT 0,
  anchor_end       INTEGER NOT NULL DEFAULT 0,
  source_level     TEXT NOT NULL CHECK (source_level IN ('S1','S2','S3','S4')),
  confidence       REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  visibility       TEXT NOT NULL CHECK (visibility IN ('L1','L2','L3','L3D','L4','L5')),
  strength         REAL NOT NULL DEFAULT 1.0,
  last_recall_at   REAL,
  embedding        BLOB,
  embedding_model  TEXT,
  injected_suspect INTEGER NOT NULL DEFAULT 0,
  gray_forgotten   INTEGER NOT NULL DEFAULT 0,   -- 灰色遗忘降权（manage.forget gray，可恢复）
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_superseded ON memory_entries (supersedes) WHERE supersedes IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entries_visibility ON memory_entries (tenant_id, visibility);
CREATE INDEX IF NOT EXISTS idx_entries_active ON memory_entries (tenant_id, subject, predicate) WHERE valid_to IS NULL;

CREATE TABLE IF NOT EXISTS association_edges (
  edge_id     TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  edge_type   TEXT NOT NULL CHECK (edge_type IN
    ('entity','temporal','contextual','supersedes','semantic','causal','derived','conflict')),
  src_id      TEXT NOT NULL,
  dst_id      TEXT NOT NULL,
  weight      REAL NOT NULL DEFAULT 1.0,
  confidence  REAL NOT NULL DEFAULT 1.0,
  evidence    TEXT NOT NULL,               -- 无依据不建边
  valid_from  TEXT NOT NULL,
  valid_to    TEXT,
  created_at  TEXT NOT NULL,
  UNIQUE (src_id, dst_id, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON association_edges (src_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON association_edges (dst_id);

CREATE TABLE IF NOT EXISTS intent_details (
  entry_id          TEXT PRIMARY KEY REFERENCES memory_entries(entry_id) ON DELETE CASCADE,
  state             TEXT NOT NULL DEFAULT 'candidate'
                    CHECK (state IN ('candidate','active','expired','completed')),
  ttl_expires_at    TEXT,
  visibility        TEXT NOT NULL DEFAULT 'L1' CHECK (visibility IN ('L1','L2','L3','L3D','L4','L5')),
  expected_response TEXT NOT NULL DEFAULT 'solution',
  origin            TEXT NOT NULL CHECK (origin IN ('declared','agent_candidate')),
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intents_state ON intent_details (state, ttl_expires_at);

CREATE TABLE IF NOT EXISTS published_items (
  item_id           TEXT PRIMARY KEY,
  entry_id          TEXT NOT NULL REFERENCES memory_entries(entry_id) ON DELETE CASCADE,
  level             TEXT NOT NULL CHECK (level IN ('L4','L5')),
  allowed_purposes  TEXT NOT NULL DEFAULT '[]',
  review_status     TEXT NOT NULL DEFAULT 'draft' CHECK (review_status IN ('draft','published','withdrawn')),
  summary           TEXT NOT NULL DEFAULT '',
  published_at      REAL,
  withdraw_deadline REAL                        -- 72h 无痕撤回窗（§11.1-②）
);
CREATE INDEX IF NOT EXISTS idx_published_status ON published_items (review_status, level);

CREATE TABLE IF NOT EXISTS authorization_records (
  auth_id         TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL DEFAULT '',
  domain_id       TEXT NOT NULL DEFAULT '',
  grantee         TEXT NOT NULL,
  object_ref      TEXT NOT NULL,               -- 条目/主题域/时间窗
  level           TEXT NOT NULL CHECK (level IN ('L1','L2','L3','L3D','L4','L5')),
  purpose         TEXT NOT NULL,
  allowed_purposes TEXT NOT NULL DEFAULT '[]', -- 允许目的集：公开面裁决谓词（约束 16）
  expires_at      REAL,
  status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked','expired')),
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_authz_grantee ON authorization_records (grantee, status);

CREATE TABLE IF NOT EXISTS visibility_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id    TEXT,
  domain_id   TEXT NOT NULL,
  from_level  TEXT,
  to_level    TEXT NOT NULL,
  confirm_ref TEXT,
  created_at  TEXT NOT NULL
);
