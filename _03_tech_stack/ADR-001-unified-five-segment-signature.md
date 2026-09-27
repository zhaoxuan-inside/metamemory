# ADR-001 统一五段签名：evolution 入列、tenant 入 context（_03_tech_stack）

> 状态：accepted　日期：2026-09-27

## 背景

hs-project-skill SOP 强制方法签名 `boolean method(input, output, context, metrics, evolution)`（统一形态供框架 AOP 统一拦截：进出入计时、日志收集经 metrics 由框架完成；业务结果从 output 返回）。而文档族 `business/框架层_开源/1_框架设计.md` §一-5 与 `2_框架详细设计.md` §3.1 定义步骤签名 `boolean step(input, output, context, tenant, metrics)`（五参全 map）——evolution 缺席、tenant 独立成参。本次按 SOP 重排全文档族触发裁决（见 _02_principle/02_conflict_analysis.md C-A）。

## 决策

全项目方法采用 SOP 五段签名：

```text
boolean method_name(input, output, context, metrics, evolution)
```

- Python 形态：`def method_name(input, output, context, metrics, evolution) -> bool`（input/output 在容器边界为 dict map，操作段在容器内构造强类型）；
- `tenant` 不再作为独立参数：`TenantCtx(tenant_id, domain_id, actor, trace_id)` 放入 `context.tenant`，由框架 contextvar 贯穿（框架 LLD §九机制保留，仅参数面收敛）；
- `evolution` 为第五段：业务方法结束前必须回填（false 时 reasonCode/reason 必填）；与异常策略引擎对接——业务方法可 `raise Undecidable(reason_code)`，policy 引擎按错误类型路由处置（Undecidable → abstain 挂 pipeline_review_queue，A15 语义不变）。

## 理由

- SOP 冲突裁决顺序：用户当场指令（按 SOP 重排）> SOP > 其他——签名冲突归 SOP；
- 统一五段是 AOP（进出入计时/日志/指标统一收集）与演化闭环（evolution 统一回收、失败原因链 cause[] 逐层合并）的前提；
- tenant 以 contextvar 贯穿本就是框架机制（security/context.py），独立参数冗余且易漏传；
- 异常策略引擎的处置依据（错误类型 + 原因码）与 evolution 契约天然同构，合并后业务代码零重复。

## 备选方案

| 方案 | 未选原因 |
|---|---|
| 保留框架签名（tenant 入列、无 evolution） | 违背 SOP 强制约束；失去演化反馈的统一回收通道；AOP 拦截面缺一段 |
| 双签名并存（框架层 map 签名 + 业务层五段签名） | 两套形态使 AOP/防重/登记复杂度翻倍，违背"统一形态"初衷 |

## 后果

- 正面：签名全项目唯一；失败原因链/重试建议/演化信号有了统一容器；_07 登记口径唯一；
- 负面/代价：框架层 LLD §3.1 示例签名需按本 ADR 修正（tenant→context.tenant、+evolution）；已发布的框架调用方需适配（当前处于骨架期，无存量调用方）；
- 对 _04 设计的影响：container/runner 五段执行序不变；policy 引擎输入从"错误类型"扩展为"错误类型 × evolution"；_06 内部接口按五段全量重写。
