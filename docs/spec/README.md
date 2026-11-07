# 记忆日记的 AuSM Profile

**自传快照记忆（Autobiographical Snapshot Memory，AuSM）**的核心规范、交换 Schema、一致性契约与实现索引，现由独立仓库 [mrzhsz3210/AuSM](https://github.com/mrzhsz3210/AuSM) 维护。

本目录只保留记忆日记作为首个参考实现的具体 Profile，不再承载 AuSM Core 本体。

## 文档

| 文档 | 作用 |
| --- | --- |
| [AuSM 独立仓库](https://github.com/mrzhsz3210/AuSM) | 路线首页、作者与版本信息、后续实现和一致性进度 |
| [AuSM Core Spec v0.1](https://github.com/mrzhsz3210/AuSM/blob/main/docs/spec/AuSM-Core-v0.1.md) | 平台无关的问题定义、核心对象、不变量、宿主能力与生命周期 |
| [AuSM Core JSON Schema v0.1](https://github.com/mrzhsz3210/AuSM/blob/main/schemas/AuSM-Core-v0.1.schema.json) | `AuSMExchange/0.1` 的机器可读交换模型 |
| [Memory Diary Profile v0.1](MemoryDiaryProfile-v0.1.md) | 记忆日记 v0.7.2 的完整物理 Schema、AstrBot 生命周期、HMAC、FTS、迁移包与实现边界 |

## Core 与 Profile 的关系

- **Core 回答“什么是 AuSM”**：不要求 AstrBot、QQ、Python、ZIP、特定数据库或特定模型厂商。
- **Memory Diary Profile 回答“记忆日记怎样实现 AuSM”**：记录当前插件真实使用的文件、字段、触发条件、预算与兼容规则。
- 新平台可以实现自己的 Profile，不必复制 AstrBot 的消息格式，也不必采用当前插件的每 4 篇重写、ZIP 整包重写或 FTS 参数。

当前准确口径是：**AuSM 已具备平台无关核心草案和一个真实参考实现，正在向多宿主互操作与实证评估推进。**第二宿主、跨宿主黄金语料、自动化 Core-Interchange 与同预算论文评估尚未完成，具体状态以独立仓库公开记录为准。
