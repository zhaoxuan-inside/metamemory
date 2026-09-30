# 02 模块间接口与数据模型（_06_interface_design）

> 状态：confirmed　更新：2026-09-27
> 所有方法签名遵循 ADR-001 **v2 六段规范**（2026-09-27 用户裁定融合）：`boolean method_name(input, output, context, tenant, metrics, evolution)`。骨架期 Python 形态 input/output 为 dict（容器边界全 map），操作段在容器内构造强类型；tenant 为 TenantCtx（数据主权边界，方法内只读；RLS 会话绑定由框架经 contextvar 同步）。

## 1. 内部接口清单

| 接口ID | 签名 | category | 层 | 模块 | 对应任务 | 备注 |
|---|---|---|---|---|---|---|
| I-001 | `boolean extract_by_mappings(input, output, context, tenant, metrics, evolution)` | data | framework | contract | T-003 | 映射提取：required/default/类型断言 |
| I-002 | `boolean flatten_by_mappings(input, output, context, tenant, metrics, evolution)` | data | framework | contract | T-003 | 产出对象拍平写回 output map |
| I-003 | `boolean assert_fields(input, output, context, tenant, metrics, evolution)` | data | framework | contract | T-003 | 契约断言；判定型步骤强制 confidence |
| I-004 | `boolean resolve_policy(input, output, context, tenant, metrics, evolution)` | data | framework | container | T-004 | 错误类型×处置表；Undecidable→abstain |
| I-005 | `boolean run_step(input, output, context, tenant, metrics, evolution)` | orchestration | framework | container | T-005 | 容器五段执行：提取→封装→操作→拍平→策略 |
| I-006 | `boolean detect_cycle(input, output, context, tenant, metrics, evolution)` | algorithm | framework | engine | T-006 | 图静态无环（纯计算，可单测） |
| I-007 | `boolean check_no_silent_default(input, output, context, tenant, metrics, evolution)` | data | framework | engine | T-006 | default/兜底禁直连成功终态（ADR-003） |
| I-008 | `boolean validate_graph(input, output, context, tenant, metrics, evolution)` | orchestration | framework | engine | T-006 | 编译五关汇总 |
| I-009 | `boolean run_flow(input, output, context, tenant, metrics, evolution)` | orchestration | framework | engine | T-007 | lite 内嵌执行器：就绪调度/条件推进/检查点 |
| I-010 | `boolean cosine_top_k(input, output, context, tenant, metrics, evolution)` | algorithm | framework | stores | T-008 | 内存向量相似 top-k（纯计算） |
| I-012 | `boolean fastpath_store(input, output, context, tenant, metrics, evolution)` | data | core | ingest | T-013 | 原文层同步落袋（零 LLM，快通道） |
| I-013 | `boolean normalize_event(input, output, context, tenant, metrics, evolution)` | data | core | ingest | T-013 | 事件归一（形态标记/时间锚定） |
| I-014 | `boolean segment_units(input, output, context, tenant, metrics, evolution)` | data | core | ingest | T-013 | 单元切分（写侧第一决策；span 保底可逆） |
| I-015 | `boolean pre_gate(input, output, context, tenant, metrics, evolution)` | data | core | ingest | T-013 | 安全/PII 门 fail-closed + 软信号 confidence |
| I-016 | `boolean dedupe_route(input, output, context, tenant, metrics, evolution)` | data | core | ingest | T-013 | 查重短路 → switch 路由 new/duplicate/abstain |
| I-017 | `boolean select_worthiness(input, output, context, tenant, metrics, evolution)` | data | core | ingest | T-013 | 五维信号+反面清单 → 值得度（四态前置） |
| I-018 | `boolean extract_deep(input, output, context, tenant, metrics, evolution)` | orchestration | core | ingest | T-014 | 深提取编排：网关失败→DependencyError 降级链 |
| I-019 | `boolean anchor_span(input, output, context, tenant, metrics, evolution)` | algorithm | core | ingest | T-014 | 引文锚定（模糊 span 定位，纯计算） |
| I-020 | `boolean verify_entailment(input, output, context, tenant, metrics, evolution)` | data | core | ingest | T-014 | NLI 忠实校验骨架（entail/contradict/neutral） |
| I-021 | `boolean route_score(input, output, context, tenant, metrics, evolution)` | data | core | ingest | T-014 | 四态路由：提取/折叠/留原文/**abstain** |
| I-022 | `boolean organize_links(input, output, context, tenant, metrics, evolution)` | orchestration | core | ingest | T-015 | 双时态落库+确定性建边（时序/情境） |
| I-023 | `boolean coarse_filter(input, output, context, tenant, metrics, evolution)` | data | core | protocol | T-011 | 裁决粗筛：租户+档位谓词 |
| I-024 | `boolean fine_check(input, output, context, tenant, metrics, evolution)` | data | core | protocol | T-011 | 精裁：角色规则/撤回过期/L1 出域拦截 |
| I-025 | `boolean purpose_gate(input, output, context, tenant, metrics, evolution)` | data | core | protocol | T-011 | 公开层目的闸：允许目的集×声明目的 |
| I-026 | `boolean arbiter_decide(input, output, context, tenant, metrics, evolution)` | orchestration | core | protocol | T-011 | VisionArbiter 唯一裁决入口（粗筛→精裁→目的闸） |
| I-027 | `boolean protocol_add(input, output, context, tenant, metrics, evolution)` | orchestration | core | protocol | T-012 | add 门面：计量→幂等→快通道→入队 |
| I-028 | `boolean protocol_search(input, output, context, tenant, metrics, evolution)` | orchestration | core | protocol | T-012 | search 门面：召回→融合→裁决→组装出口 |
| I-029 | `boolean merge_candidates(input, output, context, tenant, metrics, evolution)` | data | core | query | T-016 | 五路候选合并去重 |
| I-030 | `boolean rrf_fuse(input, output, context, tenant, metrics, evolution)` | algorithm | core | query | T-016 | RRF 融合（1/(k+rank)，纯计算） |
| I-031 | `boolean budget_pack(input, output, context, tenant, metrics, evolution)` | algorithm | core | query | T-016 | Token 预算内择优装入+溯源引用 |
| I-032 | `boolean query_recall(input, output, context, tenant, metrics, evolution)` | orchestration | core | query | T-016 | 五路并行召回编排（lite 骨架） |

**豁免清单（不入 _07）**：api REST handler（FastAPI 路由）、bootstrap 组合根、StepRegistry 注册表与 Store 槽位 Protocol 定义、InMemory 数据结构方法、Metrics/TenantCtx 构造器、domain dataclass、测试函数（完整清单登记 `_00.conventions.signature_exemptions`）。

**方法总数与实况**：上列 31 个接口（I-011 已撤销，registry 为 infra 豁免件）+ 快通道 I-012 在实现中均已落地并登记 `_07_method_idx.json`；run_step/run_flow 内部的私有 helper（`_apply_policy`、`_advance` 等）属于单一方法的实现分解，不单独登记。

## 2. 关键接口契约

### I-027 `boolean protocol_add(input, output, context, tenant, metrics, evolution)`

| 段 | 类型 | 约束 |
|---|---|---|
| input | dict（_06/01 API-001 请求体） | 只读；source_uri 参与幂等 |
| output | dict {task_id, sse_url, fastpath{stored, searchable_in}} | 受理回执 |
| context | Context（deps: 各 Store 槽位句柄；config: 档位配额） | 只读 |
| tenant | TenantCtx（key 派生，个人域 1:1 租户） | 只读；数据主权边界 |
| metrics | Metrics | 记录 `add.accepted`、`fastpath.latency_ms`；进出入由框架写 |
| evolution | Evolution | false 时 reasonCode ∈ {QUOTA_EXCEEDED, IDEM_CONFLICT, VALID_FAILED} |

**前置条件**：key 已认证且 scope 覆盖写入档位。**后置条件**：原文层已落袋（快通道）且 ingest 任务已入队（同事务）。**失败语义**：配额封顶→false(QUOTA_EXCEEDED)；幂等冲突→false(IDEM_CONFLICT)（附原 task_id 于 output）；快通道写失败→false(FASTPATH_FAILED) 不产生"已记住"错觉。

### I-026 `boolean arbiter_decide(input, output, context, tenant, metrics, evolution)`

| 段 | 类型 | 约束 |
|---|---|---|
| input | dict {candidates[], querier_ctx, purpose?} | 只读；candidates 携带 visibility/source_level；querier 缺省时由 tenant 派生 |
| output | dict {allowed[], denials[], audit_ref} | denials 逐条携带 reasonCode |
| context | Context（deps: vision_rules 授权句柄） | 只读 |
| tenant | TenantCtx | 只读；跨租户粗筛谓词输入 |
| metrics | Metrics | `arbiter.allowed` / `arbiter.denied{reason}` / `arbiter.purpose_gate_blocked` |
| evolution | Evolution | false 仅在裁决器自身故障（SYS-5000）；业务拒绝是 true + denials 非空 |

**前置条件**：查询者上下文来自 key 派生（不接受自报）。**后置条件**：放行集写审计流（全量不采样）。**失败语义**：安全/PII 门 fail-closed 只能收紧；任何一级判不了 → abstain 挂人裁（无默认放行分支）。**唯一入口**：所有读取路径必经本方法（公理 6：视野无特殊通道；业务层禁直连读取面，ADR 16）。

### I-021 `boolean route_score(input, output, context, tenant, metrics, evolution)`

**input**：{atom_candidates[]（含 anchor/nli/confidence/value_score）}；**output**：{route: extract|fold|keep_raw|abstain, routed[], review_ref?}。**失败语义**：`abstain` 是**合法 true 路径**（route=abstain + review_ref 指向 pipeline_review_queue，原因码必填）；仅输入非法时 false(VALID_FAILED)。**约束**：低置信（< 阈值）不得路由 extract；反面清单命中 → keep_raw/fold；把握不足 → abstain（禁默认落置）。

### I-009 `boolean run_flow(input, output, context, tenant, metrics, evolution)`

**input**：{pipeline, version, params, config_snapshot}；**output**：{task_status, cursor(活跃节点集), outputs{node: map}}。**约束**：节点完成=原子提交（输出+状态+后继激活同事务）；任意节点 abstained → 后继不激活或汇入 review 节点；join/wait 必有超时（防孤儿）。**full 档**：本接口签名不变，实现替换为 PG SKIP LOCKED 派发 + 租约 + llm_calls 幂等缓存。

## 3. 数据模型 Schema（核心 DDL 摘要，完整见 metamemory/data/*.sql）

```sql
-- 核心层：双时态语义原子（数据模型一次定型，ADR 2/产品 ADR 2）
CREATE TABLE memory_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL, domain_id uuid NOT NULL,
  subject_id uuid, predicate text NOT NULL, object_value text NOT NULL,
  entry_type text NOT NULL CHECK (entry_type IN ('preference','fact','promise','event','intent','pattern')),
  valid_from timestamptz NOT NULL, valid_to timestamptz,          -- 世界时间
  learned_at timestamptz NOT NULL DEFAULT now(),                   -- 系统时间
  supersedes uuid REFERENCES memory_entries(id),                   -- 更新=关闭不删除
  anchor_episode uuid NOT NULL REFERENCES memory_episodes(id),     -- 无引文不记忆
  anchor_span int4range NOT NULL,
  source_level text NOT NULL CHECK (source_level IN ('S1','S2','S3','S4')),
  confidence numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  visibility text NOT NULL CHECK (visibility IN ('L1','L2','L3','L3D','L4','L5')),
  strength numeric(6,3) DEFAULT 1.0, last_recall_at timestamptz,   -- 再巩固窗口锚点
  embedding vector(1024), embedding_model text,
  injected_suspect boolean NOT NULL DEFAULT false,                 -- 注入疑标记（§3.5）
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_entries_superseded ON memory_entries (supersedes) WHERE supersedes IS NOT NULL;
CREATE INDEX idx_entries_visibility ON memory_entries (tenant_id, visibility);

-- 关联边：八类单表（血缘=type='derived'，选型 #10）
CREATE TABLE association_edges (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL, edge_type text NOT NULL
    CHECK (edge_type IN ('entity','temporal','contextual','supersedes','semantic','causal','derived','conflict')),
  src_id uuid NOT NULL, dst_id uuid NOT NULL,
  weight numeric(4,3), confidence numeric(4,3),
  evidence jsonb NOT NULL,                                          -- 无依据不建边
  valid_from timestamptz NOT NULL, valid_to timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (src_id, dst_id, edge_type)
);

-- 框架层：abstain 一等状态（task_steps 节选，见 data/framework_tables.sql 全量）
-- task_steps.status ∈ ('ready','running','waiting','succeeded','failed','skipped','abstained')
-- task_steps.abstain_reason text NOT NULL（abstained 时，禁空串）
```

其余表：memory_episodes（原文层+切分引用）、segments（span+边界依据+参数版本）、entities/entity_aliases、intent_details（状态机位+三旋钮）、published_items（审阅状态+撤回窗）、authorization_records（三元组+允许目的集）、vision_rules、pipeline_review_queue、conflict_queue、feedback_events、evolution_params、prompts、audit_log/metering_events（框架）、tasks/task_steps/pipelines/data_contracts/component_registry/llm_calls（框架）。与 _04 数据流一致。

## 4. 接口演进规则

- 接口变更必须同步更新本文件与 `_07_method_idx.json`；
- 不兼容变更必须新增接口并废弃旧接口（_07 标记 deprecated + replaced_by；REST 走 `/v2` 与 sunset 头）；
- 数据契约破坏性变更 = 新契约版本 + 新步骤版本（ADR-005 禁原地改）；
- Store 槽位 Protocol 是框架↔实现的契约：新增槽位需更新 ADR-004 并过降级矩阵评估。
