# metamemory_console · Web 管理控制台（metamemory 的同级项目）

**可视化配置 + 可视化追踪**的管理面：纯 Python 标准库实现（零第三方依赖、离线可用），对 `metamemory/bootstrap.py` 组合根做运行时管理。

## 启动

```bash
python3 metamemory_console/server.py --port 8100
# 打开 http://127.0.0.1:8100
```

## 功能面

| 页签 | 能力 |
|---|---|
| 总览 | 资产 KPI（原文情境/语义原子/关联边/任务/待人裁）、业务指标 SVG 柱状图（方法经 metrics 写入，计时由框架 AOP 自动采集）、已注册步骤清单（六段签名 name@version） |
| 任务追踪 | 写入记忆（受理 → ingest 流程同步驱动）→ 任务列表 → **节点级追踪**（每个节点的状态机与输出 JSON，颜色区分 succeeded/abstained/failed）、复核队列（abstain 挂人裁汇口） |
| 检索验证 | search 全链路验证：查询 + 检索意图三分 + 目的（公开面目的闸），结果带来源级别/置信/视野/溯源 span |
| 配置 | 运行时配置表单（profile / quota / **inference 推理网关** / recall / memory / governance），保存即生效：网关按新配置重建、配额/检索参数热更新；持久化 `console_state.json` |
| 评测基线 | 读取 `metamemory-eval/reports/last_report.json`：路由一致率 / recall@5 / MRR / 拒答 / 注入拦截 |

**关键闭环**：配置页填好推理网关（`inference.base_url` + `model`，任意 OpenAI 兼容端点——vLLM/Ollama/Ark/OpenAI），深提取即走真实强模型（ADR-006 唯一最佳路径）；未配置时系统**显式失败**（任务 failed + 复核/死信），控制台顶部给出醒目提示——不静默降质。

## 测试

```bash
python3 -m unittest metamemory.tests.test_console -v
```

（含真实 HTTP 冒烟：状态 API、写入→追踪、检索验证、配置热更新与网关重建。）
