-- MetaMemory SQLite 档 schema·框架层（lite 档真源，ADR-004 降级矩阵 SQLite 等价实现）。
-- 与 PG DDL（framework_tables.sql）表名与语义对齐：
--   uuid→TEXT, timestamptz→TEXT(ISO8601)/REAL(epoch), jsonb/json→TEXT(JSON),
--   boolean→INTEGER, vector→BLOB(float32), 月分区→单表+索引。
-- 由 metamemory_framework.stores.sqlite_impl.SqliteDb 装载（CREATE TABLE IF NOT EXISTS，幂等）。

-- ===== 框架层 =====


CREATE TABLE IF NOT EXISTS kv_counters (
  name  TEXT PRIMARY KEY,
  value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
  id               TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  pipeline         TEXT NOT NULL,
  pipeline_version INTEGER NOT NULL,
  priority         INTEGER NOT NULL DEFAULT 30,
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','running','waiting','succeeded','failed','partial','canceled')),
  idempotency_key  TEXT,
  params           TEXT NOT NULL DEFAULT '{}',
  config_snapshot  TEXT NOT NULL DEFAULT '{}',
  lease_owner      TEXT,
  lease_expires_at REAL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_tasks_dispatch ON tasks (status, priority, updated_at);

CREATE TABLE IF NOT EXISTS task_steps (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  node_id        TEXT NOT NULL,
  node_type      TEXT NOT NULL,
  step_name      TEXT,
  step_version   INTEGER,
  status         TEXT NOT NULL DEFAULT 'ready'
                 CHECK (status IN ('ready','running','waiting','succeeded','failed','skipped','abstained')),
  attempts       INTEGER NOT NULL DEFAULT 0,
  lease_owner    TEXT,
  lease_expires_at REAL,
  iteration      INTEGER,
  abstain_reason TEXT CHECK (abstain_reason IS NULL OR (abstain_reason <> '' AND abstain_reason <> 'unknown')),
  error          TEXT,
  output         TEXT,
  started_at     TEXT,
  finished_at    TEXT,
  UNIQUE (task_id, node_id, iteration)
);
CREATE INDEX IF NOT EXISTS idx_steps_ready ON task_steps (status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_steps_lease ON task_steps (lease_expires_at) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS llm_calls (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id        TEXT,
  node_id        TEXT NOT NULL,
  step           TEXT NOT NULL,
  input_hash     TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  provider       TEXT NOT NULL,
  model          TEXT,
  response       TEXT,
  cost_tokens    INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  UNIQUE (node_id, input_hash, prompt_version)   -- 幂等重放键（A7）
);

CREATE TABLE IF NOT EXISTS pipeline_review_queue (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id   TEXT NOT NULL,
  task_id     TEXT,
  node_id     TEXT,
  reason_code TEXT NOT NULL CHECK (reason_code <> '' AND reason_code <> 'unknown'),
  payload     TEXT,
  retry_state TEXT NOT NULL DEFAULT 'pending',
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  channel    TEXT NOT NULL,
  payload    TEXT NOT NULL,
  published  INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  url           TEXT NOT NULL,
  payload       TEXT NOT NULL,
  signature     TEXT NOT NULL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_retry_at REAL,
  status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','dead')),
  created_at    TEXT NOT NULL
);

-- 审计链：append-only + prev_hash/record_hash 链式防篡改（审计自身治理，全量不采样）
CREATE TABLE IF NOT EXISTS audit_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  logged_at         TEXT NOT NULL,
  tenant_id         TEXT,
  who               TEXT NOT NULL,
  action            TEXT NOT NULL,
  object_ref        TEXT NOT NULL,
  authorization_ref TEXT,
  detail            TEXT,
  prev_hash         TEXT,
  record_hash       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metering_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id  TEXT NOT NULL,
  dimension  TEXT NOT NULL,          -- 检索/token/存储/提取条数/容量/发现位
  quantity   REAL NOT NULL,
  source     TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metering_dim ON metering_events (tenant_id, dimension, created_at);

-- ===== 槽位件（SQLite 实现专用，PG 档由对应组件承载）=====

-- 全文槽位（FTS5；PG 档 = FTS+zhparser）
CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs USING fts5(text, ns UNINDEXED, doc_id UNINDEXED);

-- 图谱槽位（邻接表；PG 档 = 递归 CTE/图库）
CREATE TABLE IF NOT EXISTS graph_edges (
  src       TEXT NOT NULL,
  dst       TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  props     TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (src, dst, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_graph_src ON graph_edges (src, edge_type);

-- 向量槽位（BLOB float32；PG 档 = pgvector）
CREATE TABLE IF NOT EXISTS vectors (
  ns     TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  vec    BLOB NOT NULL,
  PRIMARY KEY (ns, doc_id)
);

-- 锁槽位（CAS + TTL；PG 档 = advisory lock）
CREATE TABLE IF NOT EXISTS locks (
  key        TEXT PRIMARY KEY,
  owner      TEXT NOT NULL,
  expires_at REAL NOT NULL
);

-- 任务队列槽位（CAS 认领 + 租约；PG 档 = SKIP LOCKED）
CREATE TABLE IF NOT EXISTS queue_items (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  queue           TEXT NOT NULL,
  payload         TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','done','dead')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  lease_owner     TEXT,
  lease_expires_at REAL,
  next_retry_at   TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_poll ON queue_items (queue, status, next_retry_at);
