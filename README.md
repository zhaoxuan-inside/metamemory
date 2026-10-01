# metamemory

元记忆，AI时代的唯一记忆选择。

MetaMemory 是 AI 时代的记忆中间层（"所有 AI Agent 的海马体与外脑"）：**框架层 + 核心层开源同仓、业务层闭源独立仓**（三层两仓，ADR-002）。核心层只做两件事——**接收信息（add）与查询信息（search）**（ADR 14），附带视野裁决/审计/计量横切。

## 项目结构（hs-project-skill 十一件套）

> 一切开发先自 `_00_project_idx.json` 读起。

| 路径 | 内容 |
|---|---|
| `_00_project_idx.json` | 项目总索引（Agent 阅读起点） |
| `_01_business/` | 需求原理：需求/EARS 验收标准/领域概念表 |
| `_02_principle/` | 候选方案原理级对比 + **全文档族冲突分析报告** |
| `_03_tech_stack/` | 选型清单 + ADR-001~005 |
| `_04_framework_design/` | 三层两仓架构/模块职责/数据流 |
| `_05_function_design/` | 功能矩阵 + 任务卡（T-001~T-022）+ 里程碑 |
| `_06_interface_design/` | 对外 API（add/search/context/manage 四动词）+ 内部六段签名 + DDL |
| `_07_method_idx.json` | 方法索引（29 个五段方法，防重复开发） |
| `_09_project_dict.json` | 项目词典（37 词条，一物一名） |
| `_10_change_log.json` | 变更日志（按需求记账） |
| `metamemory/` | 代码：`src/metamemory_framework`（框架层）+ `src/metamemory`（核心层）+ flows/contracts/api/data/tests |
| `metamemory-eval/` | 同级评测项目：golden/recall/injection 三套件 + 阈值门禁（`run_eval.py --gate`） |
| `metamemory_console/` | 同级 Web 管理控制台：可视化配置（推理网关热更新）+ 可视化追踪（任务/节点级状态机、复核队列、指标） |
| `business/` | 原始文档族 v0.7（本 SOP 结构的素材源，保留追溯） |

## 快速开始（lite 档骨架，零第三方依赖）

```bash
# 运行测试（102 个用例，stdlib unittest）
python3 -m unittest discover -s metamemory/tests -t metamemory/tests

# 效果评测（四态路由/检索召回/注入拦截 + 门禁）
python3 metamemory-eval/run_eval.py --gate

# Web 管理控制台（可视化配置 + 可视化追踪）
python3 metamemory_console/server.py --port 8100   # http://127.0.0.1:8100

# 组合根冒烟：装配→装载流程→编译校验
cd metamemory && python3 bootstrap.py

# API 面（需 pip install fastapi uvicorn）
cd metamemory && python3 bootstrap.py  # fastapi 就绪时自动 serve :8000
```

## 对外协议速览（详见 _06）

```text
POST /v1/memories          add：受理即落袋（快通道 <100ms），202 + task_id + SSE
POST /v1/search            search：五路召回 → RRF → VisionArbiter 裁决（key 即查询者）
POST /v1/context           Token 预算组装（可选动词，add/search 独立闭环）
POST /v1/manage/{action}   correct / visibility / authz / forget（写侧治理四子动作）
```

## 开发纪律

方法统一六段签名 `boolean method(input, output, context, tenant, metrics, evolution)`（ADR-001 v2，2026-09-27 用户裁定融合）；
三分类登记 `_07`；命名查 `_09`；abstain 是一等处置（ADR-003）；外部组件一律经 Store 槽位/防腐层（ADR-004）。
Agent 禁止 `git push`，推送由人工完成。
