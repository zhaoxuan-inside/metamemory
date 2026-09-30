# 01 选型清单与依赖约束（_03_tech_stack）

> 状态：confirmed　更新：2026-09-27
> 来源：`business/核心层_开源/2_核心层技术选型.md` v0.7（#1-27 决策）、`business/框架层_开源/2_框架详细设计.md` v0.1（依赖方向）、`business/业务层_闭源/2_业务层技术选型.md`（X1-X4）。原理依据见 _02。

## 1. 选型清单

| # | 领域 | 候选 | 结论 | 关键理由 | 决策出处 / ADR |
|---|---|---|---|---|---|
| 1 | 方法签名与运行时 | SOP 五段签名 vs 框架五参全 map | **融合六段**：`boolean m(input, output, context, tenant, metrics, evolution)`（v2 用户裁定） | 统一形态是 AOP/异常策略/演化回收的前提；tenant 主权边界进入方法级契约 | ADR-001 |
| 2 | 主语言/框架 | Python / Go / Rust | Python 3.12 + FastAPI | AI 生态+迭代速度；Rust 全量替换伤害理解管道迭代（评审已做） | 选型 #1 |
| 3 | 仓库形态 | 单体 / 三层两仓 | 框架+核心同仓两包开源；业务层独立闭源仓 | 开源边界=产品承诺；import 单向 x→core→framework | ADR-002（A14/ADR 21） |
| 4 | 关系库 | PG / 专用库组合 / SQLite | PostgreSQL 16（full）｜SQLite（lite）——DataStore 接口双实现 | 一库多能；lite 单文件零成本；RLS 是 full 档能力 | 选型 #2 |
| 5 | 向量/全文/图 | 专用中间件 vs PG 系 | pgvector / PG FTS+zhparser / 邻接表+递归 CTE（接口化可平替） | 起步免运维；多跳先小后大 | 选型 #2 |
| 6 | 外部组件接入 | 硬绑定 / 接口化 | Store 9 槽位 + 部署档位 + 允许缺失降级 | 开源极低成本部署；fork 者自由替换 | ADR-004（A10） |
| 7 | 异常与不可判定 | 默认值兜底 / 死信 / abstain 一等 | abstain 一等处置原语（挂人裁+原因码） | Turing 上界；默认兜底=静默处理（禁） | ADR-003（A15/约束 15） |
| 8 | 模块解耦 | 共享类型对象 / 数据契约 | 版本化 JSON 文档 + JSONPath 映射 + 合同测试进 CI | 上游改 schema=改一行映射；契约破坏 CI 阻断 | ADR-005（A6/#18） |
| 9 | 编排器 | Celery / Temporal / 自研 | 自研轻量（PG SKIP LOCKED+流程数据化）；演进触发→Temporal | 图执行须全权掌控准入/检查点；入队同事务 | 框架 A2；选型 #16 |
| 10 | LLM 接入 | 直连 provider / 网关 | LiteLLM Proxy + InferenceProvider 双通道（Embedded/Gateway） | 模型无关公理；推理阶梯四级；双向降级链 | 选型 #4/#13 |
| 11 | 嵌入/重排 | 云 API / 自托管 | bge-m3 + bge-reranker（接口化可换） | 自托管私有化友好；中英双语 | 选型 #5 |
| 12 | 裁决器 | OPA/Cedar / 自研 | 自研 VisionArbiter（规则数据化 json-logic） | 规则与数据模型强耦合；演进触发→Cedar/OPA | 选型 #3 |
| 13 | Agent 接入 | 自研协议 / MCP | 官方 Python SDK（FastMCP，stdio+Streamable HTTP） | 协议标准生态即插即用 | 选型 #6 |
| 14 | 网关/鉴权 | 自研 / APISIX | APISIX（OAuth2+PKCE / API Key scope→视野档位） | 鉴权限流计量插件化 | 选型 §4.10 |
| 15 | 可观测 | 自建 / OTel 栈 | OTel + Prometheus/Grafana + Loki + Langfuse + promptfoo | 全链路 trace + LLM 调用观测 + 评测回归 | 选型 §六 |
| 16 | 同步策略 | CRDT / 双时态 LWW | learned_at+内容 hash LWW 起步（同实体同谓词走 supersedes 链） | 冲突面大幅收窄；CRDT 待冲突率数据 | 选型 #14 |

## 2. 依赖与版本约束

| 依赖 | 版本 | 用途 | 许可证 | 约束 / 升级策略 |
|---|---|---|---|---|
| Python | 3.12 | 运行时 | PSF | 锁 minor |
| fastapi | ≥0.115 | api 单元（可选安装） | MIT | 允许 patch；骨架期可选依赖 |
| sqlalchemy / alembic | 2.0.x / 最新稳定 | ORM/迁移（full 档） | MIT | 锁 minor |
| postgresql + pgvector + zhparser | 16 | full 档 DataStore/Vector/FTS | PostgreSQL License | 锁 major |
| litellm | 最新稳定 | GatewayProvider | MIT | 允许 minor |
| json-logic-py / jsonpath-ng | 最新稳定 | 规则求值/契约映射 | Apache-2.0/MIT | 骨架期以内置简化实现替代，接口不变 |
| bge-m3 / bge-reranker | — | 嵌入/精排 | MIT（模型许可） | hash 增量重嵌入 |
| FastMCP（官方 SDK） | 最新稳定 | MCP 面 | MIT | 跟随官方 |
| OTel / Prometheus / Grafana / Loki / Langfuse | 最新稳定 | 可观测 | Apache-2.0 等 | SaaS 全套，私有化精简三件 |
| uvicorn | ≥0.30 | ASGI（api） | BSD | 允许 patch |

（锁文件入库；骨架期 src 零第三方依赖，tests 用 stdlib unittest。引入新依赖前先在此登记并确认许可证。）

## 3. ADR 索引

| ADR | 决策 | 状态 |
|---|---|---|
| ADR-001 | 统一六段签名（v2）：`boolean method(input, output, context, tenant, metrics, evolution)`——tenant 入列 + evolution 对接异常策略引擎 | accepted |
| ADR-002 | 三层两仓开源边界：框架+核心同仓两包、业务层独立闭源仓，import 单向 | accepted |
| ADR-003 | abstain 为一等处置原语（四态路由 + 图编译禁静默兜底 + 弃权率监控） | accepted |
| ADR-004 | Store 接口族 9 槽位 + 部署档位（full/lite/minimal）+ 允许缺失降级 | accepted |
| ADR-005 | 数据契约：版本化 JSON 文档 + JSONPath 提取/拍平 + 字段断言 + 合同测试 | accepted |

**文档族决策继承索引**（不再重复成文，按需回源）：产品 ADR 0-21（business/02_产品设计 §十三）；核心技术决策 #1-27（business/核心层_开源/2 §八）；框架架构决策 A1-A15（business/框架层_开源/1 §十一）；业务层决策 X1-X4（business/业务层_闭源/2 §三）。本目录 ADR-001~005 是本次重排**新增/收敛**的决策，与继承决策不冲突。
