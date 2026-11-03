# ADR-001 统一六段签名：五段规范与框架 tenant 段融合（_03_tech_stack）

> 状态：accepted（**v2 修订，2026-09-27 用户裁定**，取代 v1 五段结论）　日期：2026-09-27
> v1（同日早些时候）裁决为"SOP 五段 + tenant 入 context"；用户当场明确指令优先于 SOP（冲突裁决顺序第 1 条），最终融合为**六段签名**。

## 背景

hs-project-skill SOP 强制 `boolean method(input, output, context, metrics, evolution)`；文档族 `框架设计 §一-5`/`框架详细设计 §3.1` 定义 `boolean step(input, output, context, tenant, metrics)`。重排裁决过程见 `_02_principle/02_conflict_analysis.md` C-A。经用户裁定，两段融合为六段——tenant 显式入列，evolution 契约保留。

## 决策（v2，现行有效）

全项目方法采用**六段签名**：

```text
boolean method_name(input, output, context, tenant, metrics, evolution)
```

- Python 形态：`def method_name(input, output, context, tenant, metrics, evolution) -> bool`（input/output 在容器边界为 dict map，操作段在容器内构造强类型）；
- 第 4 段 `tenant: TenantCtx`（数据主权硬边界，方法内只读）：个人域 1:1 租户，组织实例=组织租户；RLS 会话绑定仍由框架经 contextvar 同步（security/context.py），业务方法一律以 tenant 参数为准，`Context` 不再承载 tenant（单一事实来源）；
- 第 6 段 `evolution`：方法结束前必须回填（false 时 reason_code 必填）；与异常策略引擎对接——`raise Undecidable(reason_code)` → policy 路由（Undecidable→abstain 挂 pipeline_review_queue，A15 语义不变）；
- 纯 algorithm 方法（cosine_top_k/anchor_span/rrf_fuse 等）不消费 tenant，但签名形态不变（空消费合法，禁止传占位对象凑数以外的省段）。

## 理由

- 用户当场明确指令优先于本 SOP 与既有 ADR（冲突裁决顺序第 1 条）；
- tenant 显式成段使"数据主权边界"在方法契约层可见可审计（v1 藏在 context 里弱化了该语义——框架层原始设计意图回归）；
- 统一六段仍是 AOP（进出入计时/日志/指标统一收集）与演化闭环（evolution 统一回收、cause[] 原因链）的前提，横切能力不受影响。

## 备选方案

| 方案 | 未选原因 |
|---|---|
| v1 五段（tenant 入 context） | 用户裁定融合签名；tenant 契约可见性弱于显式段 |
| 框架原签名（无 evolution） | 演化反馈无统一回收通道；AOP 拦截面缺一段 |

## 后果

- 正面：签名全项目唯一且六段齐备；tenant 主权边界进入方法级契约；_07 登记口径唯一；
- 负面/代价：全量方法/测试/文档签名同步（本次 chg-002 一次性完成，29 个方法 + 97 个用例全绿）；调用点比五段多一个实参；
- 对 _04/_06 的影响：_06/02 内部接口清单 31 个签名全部更新为六段；支撑类型契约（Context 去 tenant 字段）同步。
