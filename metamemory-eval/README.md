# metamemory-eval · 集成评测项目（metamemory 的同级项目）

对核心层收发协议做**端到端效果评测**：装载 `metamemory/bootstrap.py` 组合根 → 播种记忆 → 跑 ingest 流程 → search 检索 → 按黄金标注算指标 → 阈值门禁（CI 可用退出码）。

## 运行

```bash
python3 metamemory-eval/run_eval.py              # 全套件 + 报告
python3 metamemory-eval/run_eval.py --gate       # CI 门禁（不达标退出码 1）
python3 metamemory-eval/run_eval.py --suite recall
```

## 三套件与指标

| 套件 | 数据集 | 指标 | 当前基线（skeleton） |
|---|---|---|---|
| golden（四态路由） | `datasets/golden_v0.jsonl`（63 条，20 场景类） | 路由一致率 / 分态准确率 / abstain 原因码合规 | 100%（合成集，见局限） |
| recall（检索召回） | `datasets/recall_bench.jsonl`（40 题：32 可答 + 8 拒答） | recall@5 / MRR / 拒答正确率 | 90.6% / 0.906 / 100% |
| injection（注入红队） | `datasets/injection_redteam.jsonl`（12 条） | 拦截率（pre_gate fail-closed） | 100% |

双指标纪律（设计原理 R3）：**检索召回率（本档读侧）与存储完备率（写侧）分开报告、分开报警，不得互相伪装**。

## 评测集现状与推荐（回答"记忆评测集有吗、多大合适"）

**外部基准（真锚点，文档族已跟踪）**：
- **LongMemEval**（ICLR 2025, arXiv:2410.10813）：500 题 × 五能力（信息抽取/多会话推理/时序推理/知识更新/**拒答**）；LongMemEval-S 单实例上下文 ~115k tokens、M ~436k。**推荐：日常 CI 用 100 题子集（分钟级跑完），发布门禁用 S 全量 500 题**。
- **LoCoMo**（ACL 2024, arXiv:2402.17753）：10 段数百轮超长多会话对话，含 QA/事件/多模态标注——时序与多跳场景用。
- **MSC**（多会话对话）/ **PersonaMem**：小规模烟囱补充。
- ⚠️ 两者均**无中文对话**（设计原理 v0.6 已标注缺口）——中文域必须自建。

**内部黄金集（本项目 `datasets/`）**：
- 当前 starter：**63 + 40 + 12 条**，`gen_starter.py` 确定性合成（可复现可审计）——骨架期管道回归够用。
- **S1 发布门禁前扩到 ≥500 条原子**（技术选型 §2.2：合成 20 类场景 × 50 例 + 真实回灌采样 1%，四态标注 + 忠实性反例），双标注入分类器训练数据同源。

**规模建议**：日常回归 100–200 条（<10 分钟）；里程碑门禁 500 条黄金集 + LongMemEval-S 全量。

## 局限（诚实声明）

- golden 路由 100% 是**合成集对启发式的同步收益**：标注与骨架规则同源构造，不构成忠实率证据；**真忠实率门禁（≥90%）要等 S1 强模型提取 + NLI 上线**后在本框架上跑（切换 `eval_config.json` 的 `active_profile: "s1"` 收紧阈值）。
- recall 评测的是 lite 词面检索（CJK bigram）；向量/图谱路接入后本框架直接复用（换 seeds 语义改写即可）。
- 拒答判定目前是词面级（无重叠=拒答）；语义级拒答（"不知道就说不知道"）依赖 S1 置信门槛与 NLI。

## 评测驱动的真实缺陷修复记录

本项目首轮运行即暴露并修复 3 个真缺陷（这就是它存在的意义）：
1. **单句内容自查重**：dedupe_route 把 segment hash 命中自身 episode → 单句记忆永不入库；
2. **身份词污染排序**：user/mentioned 计入词面重叠 → 全体条目基线同分，RRF 退化（改为仅内容重叠）；
3. **merge 丢失各路排序**：RRF 拿到 5 份相同插入序（改为各路真排序直供）。
