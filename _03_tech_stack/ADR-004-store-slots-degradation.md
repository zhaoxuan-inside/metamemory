# ADR-004 Store 接口族 9 槽位 + 部署档位 + 允许缺失降级（_03_tech_stack）

> 状态：accepted　日期：2026-09-27（继承自框架 A10 / 核心选型 #21）

## 背景

开源与私有化部署要求极低成本起步且不强绑定组件；业务代码禁止直接 import 外部组件（SOP 外部封装原则 = 框架 A11 防腐层）。

## 决策

- 一切外部组件收敛为 9 个 Store 槽位：DataStore / VectorStore / FullTextStore / GraphStore / TaskQueue / CacheStore / LockStore / ObjectStore / PubSubStore；
- 部署档位 = 槽位预设：full（PG+Redis+MinIO）/ lite（SQLite+内存缓存+本地 FS+嵌入式推理，单容器）/ minimal（SQLite+零模型）；档位是预设可覆盖，非枚举；
- **允许缺失**：Redis 缺席 → 内存 LRU+TTL；PG TaskQueue → SQLite CAS → 内存队列；pgvector → sqlite-vec → 内存向量；pg_notify → 进程内总线+扫描兜底。DataStore/FullTextStore/GraphStore/ObjectStore 为必需槽位（无降级）；
- 降级语义：功能语义不变、性能保证降级、启动标明档位；SQLite 档强制单租户声明（RLS 是 full 档能力）。

## 理由

- "lite 档本地全闭环"的叙事成立前提是组件缺失时优雅退化；fork 者可自由替换实现；
- 每个外部依赖有且只有一个封装入口（防腐层），替换成本被隔离在槽位实现内。

## 备选方案

| 方案 | 未选原因 |
|---|---|
| 硬绑定 PG+Redis+MinIO 全家桶 | 开源用户部署门槛过高；云厂商锁定 |
| 每组件独立抽象（无档位概念） | 装配矩阵组合爆炸；档位=预设+可覆盖已覆盖该需求 |

## 后果

- 正面：`docker run`/`pip install` 即用；性能敏感部署只换槽位实现；
- 负面/代价：9 个 Protocol 的接口稳定性成为契约（破坏性变更走新版本）；部分 PG 特有能力（RLS/advisory lock）在 lite 档需应用层等价实现；
- 对 _04/_06 的影响：组合根装配矩阵见 _04/01；槽位 Protocol 契约见 _06/02。
