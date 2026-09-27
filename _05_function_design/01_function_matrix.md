# 01 功能矩阵与任务卡（_05_function_design）

> 状态：confirmed　更新：2026-09-27
> 范围：本次交付 = **切片 S0（本地引擎保底档）的项目骨架**——框架层运行时 + 核心层收发协议骨架 + api 受理面。S1–S3 功能点列入矩阵不展开任务卡（随切片立项）。

## 1. 功能矩阵（模块 × 层）

| 模块 \ 层 | app（编排/入口） | domain（data） | common（algorithm） | infra |
|---|---|---|---|---|
| framework.container | run_step（五段执行编排） | resolve_policy（策略路由） | — | unified_method AOP 包装 |
| framework.contract | — | extract_by_mappings / flatten_by_mappings / assert_fields | — | — |
| framework.engine | validate_graph / GraphExecutor.run | check_no_silent_default | detect_cycle | — |
| framework.stores | — | — | cosine_top_k | 内存 Cache/Vector/Queue/Lock/PubSub 实现 |
| framework.adapters | — | — | — | Embedded/Gateway Provider 骨架 |
| framework.{security,registry,observe} | — | register_step / lookup | — | TenantCtx / Metrics 门面 |
| core.protocol | protocol.add / protocol.search / Arbiter.decide | purpose_gate / coarse_filter / fine_check | — | 授权三元组模型 |
| core.ingest | ingest 流程步骤编排（extract_deep） | fastpath_store / normalize_event / segment_units / pre_gate / dedupe_route / select_worthiness / verify_entailment / route_score | anchor_span | — |
| core.query | query.recall | merge_candidates | rrf_fuse / budget_pack | — |
| core.domain | — | 模型定义（豁免：dataclass） | — | — |
| api | REST handler（豁免五段） | — | — | — |

每个功能点可追溯到 _01 用户故事：US-1（接收/查询管道）、US-4（四动词协议面）、US-5/6（裁决与目的闸）、US-7（审计计量）、US-8（lite 档降级）。

## 2. 任务卡（S0 骨架，编号即 P7 执行顺序）

### T-001 五段支撑类型（US-1）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `metamemory/src/metamemory_framework/container/types.py` |
| 产出 | Context/TenantCtx/Metrics/Evolution/OutputMap 类型契约（ADR-001） |
| 验收方式 | tests/test_container.py::test_types |
| 依赖 | — | 预估 5min |

### T-002 unified_method 装饰器 + AOP 计时（US-1）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `container/signature.py`、`registry/registry.py`、`observe/metrics.py` |
| 产出 | boolean 五段方法注册 + 进出入计时/日志自动写 metrics（业务零埋点） |
| 验收方式 | test_container.py::test_unified_method_aop |
| 依赖 | T-001 | 预估 5min |

### T-003 数据契约映射器（US-1）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `contract/mapper.py`、`contract/assertor.py` |
| 产出 | extract_by_mappings / flatten_by_mappings / assert_fields（JSONPath 简化实现，含 required/default/类型断言） |
| 验收方式 | test_mapper.py |
| 依赖 | — | 预估 5min |

### T-004 异常策略引擎 + Undecidable（US-7）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `container/policy.py` |
| 产出 | 错误类型×处置表（retry/fallback/reply/review/**abstain**/dead_letter）；abstain 原因码必填 |
| 验收方式 | test_policy.py |
| 依赖 | T-001 | 预估 5min |

### T-005 五段执行 runner（US-1）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `container/runner.py` |
| 产出 | ①提取→②封装→③操作→④拍平→⑤异常策略；判定型步骤补 confidence |
| 验收方式 | test_container.py::test_runner |
| 依赖 | T-002/003/004 | 预估 5min |

### T-006 流程图模型 + 编译校验（US-1）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `engine/graph.py` |
| 产出 | 六型节点/条件边模型；detect_cycle；check_no_silent_default；validate_graph（图约束①-⑤） |
| 验收方式 | test_graph.py |
| 依赖 | — | 预估 5min |

### T-007 lite 内嵌执行器（US-1/8）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `engine/executor.py` |
| 产出 | 就绪集调度/条件推进/检查点（内存态，SKIP LOCKED SQL 留 full 档） |
| 验收方式 | test_graph.py::test_executor |
| 依赖 | T-005/006 | 预估 5min |

### T-008 Store 9 槽位 + 内存降级（US-8）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `stores/interfaces.py`、`stores/fallbacks.py` |
| 产出 | 9 Protocol + InMemory 实现（含 cosine_top_k）；档位装配 |
| 验收方式 | test_stores.py |
| 依赖 | — | 预估 5min |

### T-009 推理双通道骨架（US-4）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `adapters/inference.py`、`security/context.py` |
| 产出 | InferenceProvider 协议 + Embedded(Gateway) 骨架；TenantCtx contextvar |
| 验收方式 | test_stores.py::test_provider_fallback |
| 依赖 | T-001 | 预估 3min |

### T-010 领域数据模型（US-1）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `src/metamemory/domain/models.py` |
| 产出 | MemoryEpisode/Segment/MemoryEntry/AssociationEdge/Intent/PublishedItem/AuthorizationRecord |
| 验收方式 | test_domain.py |
| 依赖 | — | 预估 5min |

### T-011 VisionArbiter（US-5/6/7）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `src/metamemory/protocol/arbiter.py`、`protocol/authorization.py` |
| 产出 | coarse_filter（租户+档位）→ fine_check（规则）→ purpose_gate（允许目的集×声明目的）；decide 编排 |
| 验收方式 | test_arbiter.py |
| 依赖 | T-010 | 预估 5min |

### T-012 协议门面（US-4）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `src/metamemory/protocol/facade.py` |
| 产出 | protocol.add（快通道+入队+202 语义）/protocol.search（召回+裁决出口）——业务层唯一 import 面 |
| 验收方式 | test_protocol.py |
| 依赖 | T-011 | 预估 5min |

### T-013 接收管道步骤族（US-1）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `src/metamemory/ingest/steps.py`、`ingest/fastpath.py` |
| 产出 | fastpath_store/normalize_event/segment_units/pre_gate/dedupe_route/select_worthiness |
| 验收方式 | test_ingest.py |
| 依赖 | T-010 | 预估 5min |

### T-014 提取/校验/四态路由（US-1）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `src/metamemory/ingest/extract.py` |
| 产出 | extract_deep（网关失败→DependencyError 降级链）/anchor_span/verify_entailment/route_score（四态含 abstain） |
| 验收方式 | test_ingest.py::test_route_score_abstain |
| 依赖 | T-013 | 预估 5min |

### T-015 建网组织（US-1）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `src/metamemory/ingest/organize.py` |
| 产出 | organize_links：双时态落库+时序/情境边（确定性算子） |
| 验收方式 | test_ingest.py::test_organize |
| 依赖 | T-014 | 预估 5min |

### T-016 查询读路径（US-1/2）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `src/metamemory/query/recall.py`、`query/fusion.py`、`query/express.py` |
| 产出 | merge_candidates（五路骨架：词面/episode/时间线，向量图留位）/rrf_fuse/budget_pack（rerank 可降档开关，C-G） |
| 验收方式 | test_query.py |
| 依赖 | T-010 | 预估 5min |

### T-017 ingest 流程定义（US-1）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `metamemory/flows/core/ingest.json` 等（JSON 数据化流程图，无 YAML 解析依赖） |
| 产出 | 六型节点示例+abstain 汇聚节点（过 validate_graph）；contracts/ingest_extract_deep.json 契约样例 |
| 验收方式 | test_flows.py（装载+编译校验+契约断言） |
| 依赖 | T-006 | 预估 5min |

### T-018 DDL（US-7/8）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `metamemory/data/framework_tables.sql`、`data/core_tables.sql` |
| 产出 | 框架表（tasks/task_steps/pipelines/…）+ 核心表（附录 A 双时态组） |
| 验收方式 | 评审对照 _06/02 §3 |
| 依赖 | — | 预估 5min |

### T-019 api 受理面（US-4）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `metamemory/api/app.py` |
| 产出 | POST /v1/memories（202+task_id）/search/context/manage 路由（豁免签名，FastAPI 可选依赖） |
| 验收方式 | 无 fastapi 时跳过导入测试；契约见 _06/01 |
| 依赖 | T-012 | 预估 5min |

### T-020 组合根 + pyproject（US-8）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `metamemory/bootstrap.py`、`pyproject.toml` |
| 产出 | 档位装配→注册步骤→装载流程→serve |
| 验收方式 | python3 -c import 冒烟 |
| 依赖 | 全部 | 预估 5min |

### T-021 测试套件收口（全 US）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `metamemory/tests/`（unittest，零依赖） |
| 产出 | 上述验收测试全绿 |
| 验收方式 | `python3 -m unittest discover -s metamemory/tests` |
| 依赖 | T-001~019 | 预估 5min |

### T-022 索引登记（SOP）

| 字段 | 内容 |
|---|---|
| 涉及文件 | `_07_method_idx.json`、`_09_project_dict.json`、`_10_change_log.json`、`_00_project_idx.json` |
| 产出 | 方法/词条/变更全量登记，stats 同步 |
| 验收方式 | JSON 可解析 + 与代码对照 |
| 依赖 | T-021 | 预估 5min |

## 3. 里程碑

| 里程碑 | 包含任务 | 完成标志 |
|---|---|---|
| M0 骨架可用（本切片） | T-001~T-022 | 测试全绿；lite 内嵌执行器跑通 ingest YAML；202 受理语义成立 |
| M1 框架全量（对齐框架 LLD） | fork/loop/subflow+三层通知+租约/调用缓存 | 混沌套件 0 丢失；崩溃第 k 轮续跑 |
| M2 理解闭环（切片 S1） | 强模型提取/NLI/黄金集门禁 | 忠实率 ≥90% 才发；唤起 P99<300ms |
| M3 信任完整（切片 S2） | RLS/授权全量/意图状态机/血缘回溯 | 裁决器越权用例零放行 |
| M4 网络+开发者面（切片 S3） | MCP/SDK/L4 发布/计量对账 | 目的闸+Agent 治理基线通过 |
