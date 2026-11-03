# metamemory

元记忆，AI时代的唯一记忆选择。

MetaMemory 是 AI 时代的记忆中间层（"所有 AI Agent 的海马体与外脑"）：**框架层 + 核心层开源同仓、业务层闭源独立仓**（三层两仓，ADR-002）。核心层只做两件事——**接收信息（add）与查询信息（search）**（ADR 14），附带视野裁决/审计/计量横切。里程碑 M0–M4 全部交付（chg-005）：M1 框架全量 / M2 理解闭环 / M3 信任完整 / M4 网络与开发者面。

## 项目结构（hs-project-skill 十一件套）

> 一切开发先自 `_00_project_idx.json` 读起。

| 路径 | 内容 |
|---|---|
| `_00_project_idx.json` | 项目总索引（Agent 阅读起点） |
| `_01_business/` | 需求原理：需求/EARS 验收标准/领域概念表 |
| `_02_principle/` | 候选方案原理级对比 + **全文档族冲突分析报告** |
| `_03_tech_stack/` | 选型清单 + ADR-001~006 |
| `_04_framework_design/` | 三层两仓架构/模块职责/数据流 |
| `_05_function_design/` | 功能矩阵 + 任务卡（T-001~T-040）+ 里程碑 |
| `_06_interface_design/` | 对外 API（add/search/context/manage 四动词）+ 内部六段签名 + DDL |
| `_07_method_idx.json` | 方法索引（47 个五段方法，防重复开发） |
| `_09_project_dict.json` | 项目词典（一物一名） |
| `_10_change_log.json` | 变更日志（按需求记账，latest=chg-005） |
| `metamemory/` | 代码：`src/metamemory_framework`（框架层）+ `src/metamemory`（核心层）+ flows/contracts/api/data/tests + `config/`（配置模板） |
| `metamemory-eval/` | 同级评测项目：golden/recall/injection 三套件 + 阈值门禁（`run_eval.py --gate`，支持 `--provider gateway` 真网关模式） |
| `metamemory_console/` | 同级 Web 管理控制台：可视化配置（热更新）+ 可视化追踪（任务/节点状态机、复核队列、审计链、意图、计量） |
| `business/` | 原始文档族 v0.7（本 SOP 结构的素材源，保留追溯） |

## 三分离与零 Mock

- **配置**：`metamemory/config/metamemory.example.json` 为模板；生效文件=同目录 `metamemory.json` 或 `METAMEMORY_CONFIG` 指向任意路径；环境变量 `METAMEMORY_*` 覆盖标量。代码里只有 schema。
- **代码**：`metamemory/src/` 双包（框架层零业务 + 核心层协议本体）；流程/契约是数据（`flows/`、`contracts/`）。
- **数据**：运行时真源 = `metamemory/var/metamemory.db`（SQLite WAL，`storage.data_dir` 可指任意路径），由组合根自动建库；**代码目录零数据**（var/ 与运行配置均 gitignored）。
- **零 Mock**：运行时无任何假数据路径——存储是真 SQLite，推理是唯一网关（未配置=显式失败，ADR-006），审计链可验证；测试/评测替身是文档声明的测试工件（`_07` 豁免、ADR-006/T-033）。

## 快速开始（lite 档单容器全闭环，零第三方依赖）

```bash
# 运行测试（164 个用例，stdlib unittest）
python3 -m unittest discover -s metamemory/tests -t metamemory/tests

# 效果评测（四态路由/检索召回/注入拦截 + 门禁；--provider gateway 用真实网关）
python3 metamemory-eval/run_eval.py --gate

# Web 管理控制台（可视化配置 + 可视化追踪）
python3 metamemory_console/server.py --port 8100   # http://127.0.0.1:8100

# API 面（需 pip install fastapi uvicorn；受理 202 + worker 异步 + SSE + MCP /mcp）
cd metamemory && python3 bootstrap.py  # serve :8000

# 组合根冒烟：装配→装载流程→编译校验
cd metamemory && python3 bootstrap.py --check 2>/dev/null || cd metamemory && python3 -c "import bootstrap; bootstrap.validate_flows(bootstrap.build_composition(db_path=':memory:'))"
```

推理网关（唯一最佳路径，ADR-006）：`METAMEMORY_INFERENCE_BASE_URL`/`_MODEL`/`_EMBEDDING_MODEL` 指向任意 OpenAI 兼容端点（vLLM/Ollama/Ark/OpenAI）；未配置时深提取显式失败（原文层快通道照常），语义路按配置显式关闭。

## 对外协议速览（详见 _06）

```text
POST /v1/memories            add：受理即落袋（快通道 <100ms），202 + task_id + SSE
POST /v1/search              search：五路召回 → RRF → 精排 → VisionArbiter 裁决（key 即查询者）
POST /v1/search  {verify}    精确验证模式：逐条附 NLI 判定与引文证据
POST /v1/search  {mode}      grep：NDJSON 行输出（grep 兼容稳定契约）
POST /v1/context             Token 预算组装（可选动词）
POST /v1/manage/{action}     correct / visibility / authz / forget（写侧治理四子动作）
POST /v1/intents             意图声明（declared 直生效 / agent 只产候选）+ 确认卡片
GET  /v1/tasks/{id}/events   SSE 进度订阅（advance/progress 通知三层）
GET  /v1/audit[/verify]      审计链读取与防篡改验证（全量不采样）
GET  /v1/metering/summary    计量对账（写/读/LLM token/发现位）
GET  /v1/metrics             Prometheus 文本指标
POST /mcp                    MCP 工具面（memory.* / intent.*，JSON-RPC 2.0）
```

## 开发纪律

方法统一六段签名 `boolean method(input, output, context, tenant, metrics, evolution)`（ADR-001 v2，2026-09-27 用户裁定融合）；
三分类登记 `_07`；命名查 `_09`；abstain 是一等处置（ADR-003）；外部组件一律经 Store 槽位/防腐层（ADR-004）；
单一最佳路径无质量降级链（ADR-006）。
Agent 禁止 `git push`，推送由人工完成。
