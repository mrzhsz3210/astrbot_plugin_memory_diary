# Memory Diary Profile v0.1

**所属路线**：[AuSM Core Spec v0.1](https://github.com/mrzhsz3210/AuSM/blob/main/docs/spec/AuSM-Core-v0.1.md)  
**状态**：Draft；首个参考实现 Profile，尚未完成 Core-Interchange 验证  
**Profile 版本**：0.1  
**参考实现**：记忆日记（`astrbot_plugin_memory_diary`）v0.7.2  
**参考数据格式**：按天归档 v3；与插件版本号、Profile 版本和 AuSM Core 版本相互独立  
**文档日期**：2026-11-08  

## 摘要

本文件定义 AuSM 在记忆日记插件中的具体实现：ZIP、Markdown、JSON、HMAC、AstrBot 触发条件、渐进式交接、FTS 回源、迁移包、容量和失败边界。这里的技术参数属于 Memory Diary Profile，不是其他 AuSM 实现必须照抄的路线本质。

平台无关的对象、不变量、宿主能力契约与一致性等级以 [AuSM Core Spec v0.1](https://github.com/mrzhsz3210/AuSM/blob/main/docs/spec/AuSM-Core-v0.1.md) 为准。本 Profile 保留完整物理 schema 和实现生命周期，作为 Core 的首个现实来源与对照；Core 交换映射、黄金语料及第二宿主互操作验证尚未完成，因此本文件不单独证明 AuSM 已实现全平台通用。

关键词“必须”“应当”“可以”“不得”“不保证”用于本 Profile 的规范性要求；“当前实现”只描述参考实现，不自动构成对所有 AuSM 实现的要求。

---

# 第一部分　术语表

## 1.1 核心术语

| 术语 | 死定义 |
| --- | --- |
| **快照（Snapshot）** | 快照是系统在某一逻辑时点根据可用历史形成的、用于后续上下文交接的可替换当前视图；它不是原始历史，也不要求内容完备。 |
| **整合（Consolidation）** | 整合是把新增经历或素材转化为持久自传记录并更新当前快照的过程；历史正文不得因当前理解变化而被静默改写，当前快照则允许整体替换。 |
| **交接（Handoff）** | 交接是在上下文边界把当前快照与预算内的历史节选交付给新上下文，并保留按需读取原文通道的过程；它不是模型参数更新，也不等于完整历史注入。 |
| **四维度（Four Dimensions）** | 四维度是完整日记生成时用于自主取舍内容、顺序与篇幅的四个参考方向，不是存储 schema 中四个必填字段，也不要求每篇依次写满。 |

“整合”在实现层面必须进一步区分：

- **历史整合**：新增完整日记、简记或外部来源记录；完整篇可以吸收同场景简记，但只给旧简记分节头追加“已吸收”标记，不删除旧正文。
- **快照整合**：根据新日记与既有快照重写当前可替换段，或在规定条件下整篇重写快照；它改变“现在的理解”，不回写“当时的记录”。

“交接”与“导出”不得混用：交接面向下一轮模型上下文，导出面向用户的数据迁移与保管。

## 1.2 四个参考维度

| 维度 | 死定义 |
| --- | --- |
| **经历** | 这段时间实际发生了什么，以及足以定位该经历的原话、时间或事件。 |
| **观察** | AI 对用户、双方关系及其变化形成的认识；观察不自动等同于用户事实。 |
| **自我变化** | AI 认为自己在叙述、感受、判断或相处方式上值得留给未来自己的变化；其中拟人化内容必须保留来源边界。 |
| **未来注意事项** | 当前 AI 希望未来的自己延续、注意、核实或重新考虑的事项；它不是不可撤销的系统指令。 |

## 1.3 支撑术语

| 术语 | 定义 |
| --- | --- |
| **自传原文** | 按时间与场景保存的日记正文及其分节头，是“当时记录了什么”的历史证据。 |
| **当前快照** | 本 Profile 中的 `current_self.json`，包含当前可替换正文、有效或已作废锚点、用户确认事项与有限正文历史。 |
| **证据回源** | 从快照、搜索或批注返回具体日记条目、场景、来源与原文片段的过程。 |
| **锚点** | 用于定位或稳定记忆的具体原话、时间、事件及其来源元数据；锚点可以被作废，但作废不等于删除历史。 |
| **来源记录（provenance）** | 描述显式交代、AI 私人片段、外部写入与内部“不写”决策的签名侧车；它证明记录来源，不证明内容真实。 |
| **显式交代** | 用户明确要求记住、不要遗忘、以后注意或纠正 AI 的原话；它走确定性补齐通道，不因模型判断“不写”而静默消失。 |
| **作用域（MemoryScope）** | 记忆可读与可写边界的规范值，当前为 `private`、`group:<group_id>` 或兼容旧数据的未知群作用域。 |
| **派生索引** | 由权威原文重建、只用于定位证据的辅助结构；索引损坏或缺失不得成为修改原文的理由。 |
| **迁移语义无损** | 在限定对象集合、成功验签和成功导入的前提下，往返后的规范化内容保持一致；重签、ZIP 容器差异和明确允许的封装元数据变化不属于内容损失。 |

---

# 第二部分　数据模型

## 2.1 Core 逻辑模型

任何 AuSM 实现必须能够区分下列逻辑对象；不要求使用与本 Profile 相同的物理格式。

| 逻辑对象 | 必需语义 | 不得被解释为 |
| --- | --- | --- |
| 历史记录 | 过去某个时点形成的叙述、来源和场景 | 永远正确的当前事实 |
| 当前快照 | 最新当前理解及其修订信息 | 完整历史或人格本体 |
| 来源与证据链接 | 从当前理解回到历史记录的关系 | 对内容真实性的密码学证明 |
| 用户确认与作废状态 | 新认识是否被接受、旧锚点是否仍生效 | 对历史正文的覆盖授权 |
| 交接工作集 | 本轮按作用域和预算选出的上下文材料 | 全量持久存储 |
| 派生索引 | 可删除、可重建的检索辅助物 | 第二份事实源 |

Core 的不变量：

1. 当前理解的更新不得静默改写历史正文。
2. 快照必须允许不完整，并保留回到历史记录的通道。
3. 注入裁剪不得等价于删除持久存储。
4. 用户明确删除、明确保留期与规范公开的容量淘汰不属于“静默改写”；其实现和边界必须公开。
5. 来源标签、完整性标签与事实确认是三种不同状态，不得互相替代。

## 2.2 Profile 物理布局

当前每个 `sender_id` 对应一个签名日记 ZIP。文件名由主密钥派生，权威内容在 ZIP 内；主密钥 `.diary_key` 在 ZIP 外，不进入单用户迁移包。

```text
diary_<file_key>.zip
├── diaries/YYYYMMDD.md
├── sigs/YYYYMMDD.sig
├── images/<safe-image-name>.<png|jpg|jpeg|webp|gif>
├── sigs/images_<safe-image-name>.<ext>.sig
├── annotations.json
├── sigs/annotations.json.sig
├── provenance.json
├── sigs/provenance.json.sig
├── current_self.json
├── sigs/current_self.json.sig
├── current_self_anchor_archive.json
├── sigs/current_self_anchor_archive.json.sig
├── external_materials.json
├── sigs/external_materials.json.sig
└── 说明.txt
```

另有不随该 ZIP 删除的外部写入墓碑：

```text
provenance_tombstones_<file_key>.json
provenance_tombstones_<file_key>.json.sig
```

ZIP 中允许缺少尚无数据的可选侧车；但某个已存在的签名侧车若缺少对应签名、签名不匹配或 JSON 无法解析，读取和覆盖必须按失败关闭处理。旧格式条目可以在受控迁移中升级，不得借迁移替缺签内容重新签名。

## 2.3 通用编码、时间与字段规则

- JSON 使用 UTF-8；当前规范写序列化为 `ensure_ascii=false`、`indent=2`，不追加额外行尾。
- JSON Schema 见 §2.6。它描述**规范化后的规范写数据**，使用 JSON Schema Draft 2020-12；各文档以对应 `$defs` 为验证根。
- 对象内时间戳通常是 `%Y-%m-%dT%H:%M:%S`，导出清单使用 `%Y-%m-%d %H:%M:%S`。它们是配置时区下的本地朴素时间，不带 UTC 偏移；解释时必须结合 `diary_timezone`，不得擅自当成 UTC。
- 部分旧记录允许空时间字符串；新写记录必须写入当前本地时间。字符串后的长度限制是存储清洗上限，不是时间有效性证明。
- `missing` 表示字段或条目不存在，`null` 表示显式空值；二者不得默认视为同义。各字段是否允许 `null` 以 schema 为准。
- 读取器可以兼容旧记录缺失的可选字段；规范写器必须写出该对象标记为 `required` 的全部字段。未知输入字段可以在规范化时被丢弃，不得在未知含义下转发为权威数据。
- 字符串“字”或“字符”均指 Python Unicode 字符串长度，不是 UTF-8 字节数；字节上限会单独标明。
- `revision`、`version` 和 `since_rewrite` 是整数，不得用布尔值代替。

## 2.4 日记 Markdown 与分节

日记条目名必须匹配 `^diaries/\d{8}\.md$`，正文为 UTF-8 Markdown。一天一个条目，同日多篇以分节头追加。

当前分节头形态为：

```text
—— HH:MM · 完整[（附加说明）][ · 写于私聊] ——
—— 当天 HH:MM 简记[（已吸收）][ · 写于群聊 · 群:<group_id>] ——
—— 当天 HH:MM 随笔（无新对话·她的日常随笔） · 写于私聊 ——
```

分节解析规则：

1. 含“随笔”或“无新对话”的片段为 `life`。
2. 否则含“简记”或“补记”的片段为 `brief`。
3. 其余片段为 `full`；这也兼容没有模式标记的旧正文。
4. 含“写于群聊”的片段属于群场景；能解析群号时为 `group:<group_id>`，不能解析时为兼容作用域，不得擅自分配给现实群。
5. 含“已吸收”的简记在默认注入时跳过，原文仍保留。
6. 旧正文可以没有分节头；不得为了形式统一而重写其正文。

## 2.5 签名与密钥派生

当前用户派生签名钥匙为：

```text
user_key = HMAC-SHA256(master_key, UTF8("user-key:" + sender_id))
```

日记正文签名为：

```text
sig = HEX(HMAC-SHA256(
    user_key,
    UTF8(sender_id + "\n" + entry_name + "\n" + content)
))
```

JSON 侧车签名使用同一结构，但第三段分别是逻辑条目名，原始消息为：

```text
UTF8(sender_id) + b"\n" + UTF8(logical_entry_name) + b"\n" + raw_bytes
```

图片签名同样绑定 `sender_id`、图片 ZIP 条目名和原始二进制。签名文件保存小写十六进制 HMAC-SHA256。

签名的保证仅为：在密钥未泄露且验签成功的前提下，检测到签名域内容发生变化。签名不证明日记为真、不证明模型没有幻觉，也不替代访问控制或静态加密。

## 2.6 规范 JSON Schema 全文

以下 `$defs` 覆盖当前 AuSM 权威 JSON 文档、素材游标和导出清单。数组顺序具有语义时不得随意重排；额外跨字段约束见 §2.7。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "AuSM Memory Diary Profile v0.1 (reference implementation v0.7.2) normalized documents",
  "$defs": {
    "localTimestamp": {
      "type": "string",
      "maxLength": 30,
      "description": "配置时区下的本地朴素时间；新写值通常为 YYYY-MM-DDTHH:MM:SS，旧值可为空。"
    },
    "scope": {
      "type": "string",
      "pattern": "^(private|group:(unknown|legacy-unknown|[A-Za-z0-9_.-]{1,64}))$"
    },
    "versionRecord": {
      "type": "object",
      "additionalProperties": false,
      "required": ["revision", "text", "updated_at"],
      "properties": {
        "revision": {"type": "integer", "minimum": 1},
        "text": {"type": "string", "minLength": 1, "maxLength": 900},
        "updated_at": {"$ref": "#/$defs/localTimestamp"}
      }
    },
    "source": {
      "type": "object",
      "additionalProperties": false,
      "required": ["model", "persona", "entry"],
      "properties": {
        "model": {"type": "string", "maxLength": 120},
        "persona": {"type": "string", "maxLength": 64},
        "entry": {"type": "string", "pattern": "^diaries/\\d{8}\\.md$"}
      }
    },
    "supersession": {
      "type": "object",
      "additionalProperties": false,
      "required": ["by", "reason", "at"],
      "properties": {
        "by": {"type": "string", "maxLength": 80},
        "reason": {"type": "string", "maxLength": 200},
        "at": {"$ref": "#/$defs/localTimestamp"}
      }
    },
    "anchor": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "text", "source"],
      "properties": {
        "id": {"type": "string", "minLength": 1},
        "text": {"type": "string", "minLength": 1, "maxLength": 500},
        "source": {"$ref": "#/$defs/source"},
        "superseded_by": {"$ref": "#/$defs/supersession"}
      }
    },
    "pendingItem": {
      "type": "object",
      "additionalProperties": false,
      "required": ["text", "source", "confirmed_at"],
      "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 300},
        "source": {"type": "string"},
        "confirmed_at": {"$ref": "#/$defs/localTimestamp"}
      }
    },
    "current": {
      "type": "object",
      "additionalProperties": false,
      "required": ["revision", "text", "updated_at", "anchors", "pending_items"],
      "properties": {
        "revision": {"type": "integer", "minimum": 1},
        "text": {"type": "string", "minLength": 1, "maxLength": 900},
        "updated_at": {"$ref": "#/$defs/localTimestamp"},
        "anchors": {
          "type": "array",
          "maxItems": 200,
          "items": {"$ref": "#/$defs/anchor"}
        },
        "pending_items": {
          "type": "array",
          "items": {"$ref": "#/$defs/pendingItem"}
        }
      }
    },
    "currentSelf": {
      "type": "object",
      "additionalProperties": false,
      "required": ["version", "current", "history", "since_rewrite"],
      "properties": {
        "version": {"const": 1},
        "current": {
          "oneOf": [
            {"$ref": "#/$defs/current"},
            {"type": "null"}
          ]
        },
        "history": {
          "type": "array",
          "maxItems": 5,
          "items": {"$ref": "#/$defs/versionRecord"}
        },
        "since_rewrite": {"type": "integer", "minimum": 0}
      }
    },
    "archiveItem": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "text", "source", "archived_at", "batch"],
      "properties": {
        "id": {"type": "string", "minLength": 1},
        "text": {"type": "string", "minLength": 1, "maxLength": 500},
        "source": {"$ref": "#/$defs/source"},
        "superseded_by": {"$ref": "#/$defs/supersession"},
        "archived_at": {"$ref": "#/$defs/localTimestamp"},
        "batch": {"type": "string", "maxLength": 80}
      }
    },
    "anchorArchive": {
      "type": "object",
      "additionalProperties": false,
      "required": ["version", "items"],
      "properties": {
        "version": {"const": 1},
        "items": {
          "type": "array",
          "items": {"$ref": "#/$defs/archiveItem"}
        }
      }
    },
    "annotationReply": {
      "type": "object",
      "additionalProperties": false,
      "required": ["resolved_at", "reply_title", "summary", "confirmed_by_user", "speculation_marked"],
      "properties": {
        "resolved_at": {"$ref": "#/$defs/localTimestamp"},
        "reply_title": {"type": "string", "maxLength": 12},
        "summary": {"type": "string", "minLength": 1, "maxLength": 200},
        "confirmed_by_user": {"const": true},
        "speculation_marked": {"type": "string", "maxLength": 200}
      }
    },
    "annotation": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "round", "anchor", "entry", "created_at", "note", "is_question", "status", "reply"],
      "properties": {
        "id": {"type": "string"},
        "round": {"type": "string"},
        "anchor": {"type": "string", "minLength": 1},
        "entry": {"type": "string"},
        "created_at": {"type": "string"},
        "note": {"type": "string", "minLength": 1},
        "is_question": {"type": "boolean"},
        "status": {
          "enum": ["open", "resolved", "deferred", "pending_confirmation", "rejected"]
        },
        "reply": {
          "oneOf": [
            {"$ref": "#/$defs/annotationReply"},
            {"type": "null"}
          ]
        },
        "content_type": {
          "enum": ["fact", "observation", "speculation", "self"]
        },
        "reflection_kind": {"enum": ["ordinary", "fact"]}
      }
    },
    "annotations": {
      "type": "object",
      "additionalProperties": false,
      "required": ["version", "last_round", "last_title", "updated_at", "annotations"],
      "properties": {
        "version": {"const": 1},
        "last_round": {"type": "string"},
        "last_title": {"type": "string", "maxLength": 50},
        "updated_at": {"type": "string"},
        "annotations": {
          "type": "array",
          "maxItems": 500,
          "items": {"$ref": "#/$defs/annotation"}
        }
      }
    },
    "externalWrite": {
      "type": "object",
      "additionalProperties": false,
      "required": ["source", "ext_id", "at"],
      "properties": {
        "source": {"type": "string", "minLength": 1, "maxLength": 32},
        "ext_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "at": {"$ref": "#/$defs/localTimestamp"}
      }
    },
    "provenanceEntry": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "self": {
          "type": "array",
          "maxItems": 50,
          "items": {"type": "string", "minLength": 1}
        },
        "explicit": {
          "type": "array",
          "maxItems": 50,
          "items": {"type": "string", "minLength": 1}
        },
        "external": {
          "type": "array",
          "maxItems": 100,
          "items": {"$ref": "#/$defs/externalWrite"}
        }
      }
    },
    "noWrite": {
      "type": "object",
      "additionalProperties": false,
      "required": ["at", "reason", "cleanup_after"],
      "properties": {
        "at": {"type": "string", "minLength": 1, "maxLength": 30},
        "reason": {"type": "string", "maxLength": 100},
        "cleanup_after": {
          "oneOf": [
            {"type": "string", "maxLength": 30},
            {"type": "null"}
          ]
        }
      }
    },
    "provenance": {
      "type": "object",
      "additionalProperties": false,
      "required": ["version", "updated_at", "entries", "no_write", "no_write_streak"],
      "properties": {
        "version": {"const": 1},
        "updated_at": {"$ref": "#/$defs/localTimestamp"},
        "entries": {
          "type": "object",
          "propertyNames": {"pattern": "^\\d{8}\\.md$"},
          "additionalProperties": {"$ref": "#/$defs/provenanceEntry"}
        },
        "no_write": {
          "type": "array",
          "items": {"$ref": "#/$defs/noWrite"}
        },
        "no_write_streak": {"type": "integer", "minimum": 0}
      }
    },
    "material": {
      "type": "object",
      "additionalProperties": false,
      "required": ["id", "source", "ext_id", "scope", "text", "submitted_at", "status", "status_at", "reason", "absorbed_entry"],
      "properties": {
        "id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "source": {"type": "string", "minLength": 1, "maxLength": 32},
        "ext_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "scope": {"$ref": "#/$defs/scope"},
        "text": {"type": "string"},
        "submitted_at": {"$ref": "#/$defs/localTimestamp"},
        "status": {
          "enum": ["pending", "absorbed", "deferred", "discarded", "expired"]
        },
        "status_at": {"$ref": "#/$defs/localTimestamp"},
        "reason": {"type": "string", "maxLength": 200},
        "absorbed_entry": {
          "oneOf": [
            {"type": "string", "pattern": "^diaries/\\d{8}\\.md$", "maxLength": 40},
            {"type": "null"}
          ]
        }
      }
    },
    "externalMaterials": {
      "type": "object",
      "additionalProperties": false,
      "required": ["version", "items"],
      "properties": {
        "version": {"const": 1},
        "items": {
          "type": "array",
          "items": {"$ref": "#/$defs/material"}
        }
      }
    },
    "tombstone": {
      "type": "object",
      "additionalProperties": false,
      "required": ["source", "ext_id", "day", "at", "deleted_at", "deleted"],
      "properties": {
        "source": {"type": "string", "minLength": 1, "maxLength": 32},
        "ext_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "day": {"type": "string", "pattern": "^$|^\\d{8}$"},
        "at": {"$ref": "#/$defs/localTimestamp"},
        "deleted_at": {"$ref": "#/$defs/localTimestamp"},
        "deleted": {"const": true}
      }
    },
    "tombstones": {
      "type": "object",
      "additionalProperties": false,
      "required": ["version", "updated_at", "records"],
      "properties": {
        "version": {"const": 1},
        "updated_at": {"$ref": "#/$defs/localTimestamp"},
        "records": {
          "type": "array",
          "maxItems": 2000,
          "items": {"$ref": "#/$defs/tombstone"}
        }
      }
    },
    "materialCursor": {
      "type": "object",
      "additionalProperties": false,
      "required": ["cid", "count", "last_hash", "last_extra_hash"],
      "properties": {
        "cid": {"type": "string"},
        "count": {"type": "integer", "minimum": 0},
        "last_hash": {"type": "string", "pattern": "^$|^[0-9a-f]{32}$"},
        "last_extra_hash": {"type": "string", "pattern": "^$|^[0-9a-f]{32}$"}
      }
    },
    "exportManifest": {
      "type": "object",
      "additionalProperties": false,
      "required": ["format", "key_kind", "sender_id", "diary_file", "entry_count", "exported_at"],
      "properties": {
        "format": {"const": "memory-diary-export/2"},
        "key_kind": {"const": "user"},
        "sender_id": {"type": "string", "minLength": 1},
        "diary_file": {"type": "string", "minLength": 1},
        "entry_count": {"type": "integer", "minimum": 0},
        "exported_at": {"type": "string", "maxLength": 30}
      }
    }
  }
}
```

## 2.7 Schema 的跨字段与生成约束

JSON Schema 无法完整表达的规则如下，仍然是本 Profile 的规范部分：

### `current_self.json`

- `version` 是当前侧车结构版本，不是 AuSM 规范版本。
- `current=null` 表示尚无有效快照；此时不得伪造空正文版本。
- `history` 只保存可替换正文的最近 5 版，不包含每版的锚点和确认事项；它不是完整审计日志。
- 新锚点的规范 ID 为锚点正文 UTF-8 的 SHA-256 前 16 个十六进制字符；读取旧数据时允许保留已有非空 ID。
- 活动锚点列表最多 200 条；超出者连同原始字段移入锚点归档，而不是直接删除。
- `superseded_by` 存在时锚点不再参与默认注入，但锚点记录仍保留。
- `pending_items` 按确认先后保存，正文相同的确认事项去重；当前没有独立总条数上限。
- 滚段更新失败时，上一有效 `current` 必须保留。

### `current_self_anchor_archive.json`

- 每条归档记录是原锚点字段加 `archived_at` 与 `batch`。
- `archived_at` 是归档时间，`batch` 是本次归档批次标识；它们不修改原锚点的来源时间。
- 当前归档条目数没有硬上限，因此“活动锚点有上限”不等于“全部锚点存储有上限”。

### `annotations.json`

- 规范新生成批注的 `anchor` 最多 30 字、`note` 最多 40 字、生成标题最多 20 字；存储清洗允许旧记录拥有更长正文，`last_title` 的存储上限为 50 字。
- 新批注 ID 为 `ann_YYYYMMDD_<序号>`，冲突时追加 `_<4位hex>`；极端冲突可以使用更长后缀。旧记录空 ID 可以被兼容读取，但新记录必须生成唯一 ID。
- `entry` 的规范新值为 `YYYYMMDD.md`，与 `provenance.entries` 的键一致；锚点正文必须能在对应日记的可锚文本中找到。
- `content_type` 表示事实、观察、推测或 AI 私人内容分类；`reflection_kind=fact` 的新反思必须进入 `pending_confirmation`。
- `reply` 只在用户确认后由规范写器生成，`confirmed_by_user=true`；读取器可以兼容旧版回复缺少部分展示字段，但不得据此伪造确认。
- 总条数超过 500 时，先淘汰最旧的 `resolved`，再按时间淘汰最旧的其他记录。这是公开的有损保留策略，不得宣称批注元数据永久无损。

### `provenance.json`

- `entries` 仅在 `self`、`explicit` 或 `external` 至少一项非空时保留该日记录。
- 每日 `self` 最多 50 条且合计最多 2000 字；`explicit` 独立适用同样的 50 条与 2000 字上限。
- `external` 每日最多 100 条、全 ZIP 最多 2000 条；超限时淘汰最旧记录，因此外部幂等索引是有界窗口，不是永久去重保证。
- `no_write.at` 必填；`reason` 最多 100 字；`cleanup_after` 是预留清理字段，当前为 `null`，不表示已实现自动清理。
- `no_write` 当前没有公开总条数硬上限；`no_write_streak` 在一次成功写日记后归零。

### `external_materials.json`

- 规范素材 ID 为 `SHA256(source + "\0" + ext_id)` 的完整小写十六进制。
- 相同 ID 重放返回已有记录，不重复入队；已存在对应墓碑时不得复活正文。
- `pending` 与 `deferred` 合计最多 100 条；终态与过期记录不计入该活动队列上限。
- `status=expired` 时 `text` 必须为空；其他状态保留原文，除非用户或管理员通过另一明确流程删除。
- `status=absorbed` 时 `absorbed_entry` 指向吸收它的日记条目；其他状态为 `null`。
- 状态允许的主动迁移为：`pending/deferred → absorbed/deferred/discarded/expired`；管理员可以把 `pending/deferred` 重试为 `pending`，或明确丢弃为 `discarded`。`discarded` 不得经管理员“重试”悄悄恢复。

### `provenance_tombstones_<file_key>.json`

- `(source, ext_id)` 是逻辑去重键，只保留最近一次删除记录；`deleted` 恒为 `true`。
- `day` 为被删日记日期的八位数字，无法确定时允许空串；`at` 是原外部写入时间，`deleted_at` 是删除时间。
- 最多保留 2000 条，超限淘汰最旧记录；因此“删除后永不复活”只在墓碑仍保留且侧车可信时成立。
- 墓碑位于日记 ZIP 外，且不随单用户迁移包导出；跨设备迁移后的删除重放边界与本地不同，见第四部分。

### 素材游标

- 游标保存在用户偏好数据的 `material_cursors` 中，不在日记 ZIP 内，也不进入单用户迁移包。
- 游标键为 `cursor_ + MD5(umo)前16位hex`；MD5 在这里只用于定位，不承担安全签名功能。
- `cid` 必须匹配当前会话，`count` 不得超过当前历史长度，`last_hash` 必须匹配边界消息，否则游标失效并回退到最近 N 条历史。
- `last_hash` 与 `last_extra_hash` 是 `MD5(role + "\n" + text)`；后者用于跳过上次自动写时已经吸收、但当时尚未进入历史的当前消息。
- 最多保留 64 个场景游标，超出淘汰最旧项。

## 2.8 图片条目

图片不是 JSON 对象，字段规则由条目名和二进制约束表达：

- 条目名只能位于 `images/` 一级目录，不得包含路径穿越、反斜杠或控制字符；条目名总长度最多 160 字符。
- 扩展名只允许 `png/jpg/jpeg/webp/gif`，内容还需通过图片魔数嗅探。
- 单张图片最多 8 MiB；单 sender ZIP 内图片总量最多 256 MiB。
- 每张已存在图片必须有独立签名；缺签或验签失败按不可信处理。
- 注入时图片只转成文字占位，不以 Base64 进入上下文。

## 2.9 单用户迁移包

```text
memory_diary_export_<timestamp>_<random>.zip
├── <diary_file>          # 完整日记 ZIP 的封装副本
├── user_key.txt          # 该 sender 的派生钥匙，小写 hex
├── manifest.json         # 使用 $defs/exportManifest
└── 导入说明.txt
```

`manifest.json` 的字段均由导出器写出；导入器至少使用 `format`、`sender_id` 和 `diary_file` 定位与校验，`key_kind`、`entry_count`、`exported_at` 用于清单核对。当前还兼容只读的 `memory-diary-export/1`，其钥匙条目和派生方式不同；新导出必须使用 `/2`。

导入前必须：

1. 拒绝压缩包本体超过 32 MiB 的上传。
2. 校验外层与内层 ZIP：最多 10000 个条目；单条目解压后最多 64 MiB；总解压量最多 64 MiB；在压缩大小大于 16 字节时，单条目解压/压缩比不得超过 200。
3. 通过 CRC、日记正文、图片和已存在侧车的全部签名检查。
4. 确认主人身份和白名单；QQ 自助导入时 `sender_id` 必须与操作者一致。
5. 本机已有该用户日记 ZIP 时拒绝覆盖，而不是自动合并。

导入后使用本机派生钥匙重签。规范化对象内容应保持，签名、ZIP 压缩结果、临时文件名和容器字节可以变化。

单用户迁移包**不包含**：

- 主密钥 `.diary_key`；
- 用户偏好、查看密码、备忘与素材游标；
- Web 界面偏好、主人映射和外部写入凭证；
- 自动写日记用户表与执行状态、token 校准数据；
- ZIP 外的删除墓碑；
- 内存搜索索引及其他可重建派生物。

因此，“可导出”指本节列明的日记 ZIP 及其包含对象可被用户带走，不等于插件全部运行状态可导出。管理员整机备份是另一信任域和对象集合，不得与单用户迁移包混称。

---

# 第三部分　生命周期

## 3.1 总流程

```text
会话历史 / 显式交代 / 备忘 / 外部素材
                  ↓
        触发日记写作并选择素材
                  ↓
     写 / 不写 / 缺锚点重试 / 降级简记
                  ↓
  验签旧数据 → 更新正文与相关侧车 → 原子替换 ZIP
                  ↓
       更新当前快照（失败则保留旧版）
                  ↓
 新上下文按作用域和预算交接快照 + 轻量原文节选
                  ↓
       按需搜索原文 / 生成批注 / 用户确认
                  ↓
          导出迁移 / 明确删除 / 继续整合
```

历史写入与快照更新不是同一个原子事务：当前实现先成功保存日记，再调用模型更新 `current_self`。快照更新失败不得回滚已保存日记，也不得覆盖上一有效快照。

## 3.2 日记整合的触发条件

| 触发源 | 当前触发规则 | 请求模式与限制 |
| --- | --- | --- |
| `/日记` | 用户主动发起；第一次有日记条目之前写完整篇，之后默认简记 | `auto`；无新素材时可按配置写拟人随笔 |
| `/日记 完整` | 用户明确请求完整篇 | `full`；吸收同场景未吸收简记；达到当日完整篇上限时拒绝，不静默降级 |
| `/日记 简记` | 用户明确请求简记 | `brief`；不吸收旧简记，不占用完整篇名额 |
| 上下文哨兵 | 估算占用达到自动阈值，默认 `0.9`；每个会话代际、每个 sender 最多调度一次 | 后台请求 `full`；完整篇名额已满时降级为 `brief`；不阻塞当前聊天 |
| 每天定时写作 | 功能开启且到达配置时间，默认 `22:00`；只处理登记过的私聊用户 | 请求 `full`；当天已有完整篇或无新素材则跳过；启动晚于目标时间约 2 分钟内可补触发，超过不补当天全局触发 |
| 外部素材入队 | 已认证来源提交新素材 | 只进入 `pending`，不直接写正文；等待下一次同作用域日记写作 |
| `/reset` | 开启新上下文代际 | **只触发交接，不触发日记整合** |

提醒阈值默认 `0.75`，自动写阈值默认 `0.9`；任一设为 0 表示关闭该档，两者都为 0 表示关闭哨兵。阈值依赖 token 估算，不保证与服务商真实计费完全一致。

定时写作按用户逐个记录完成状态；同一用户同一天不重复写。处理过程中重载时，尚未处理的用户可在当天后续检查中继续；单个用户失败后不当天内无限重试。

## 3.3 素材选择、无素材与“不写”

1. 优先使用有效素材游标之后的新历史，并跳过已经吸收的 `last_extra_hash`。
2. 游标失效时回退最近 N 条；提示中必须标记可能重叠，由模型识别并跳过旧内容。
3. 日记操作指令从写作素材中剔除；群聊只围绕目标 sender 叙述，不得把其他成员发言归给该用户。
4. 私聊写作吸收私聊可用备忘；具体群只吸收本群备忘。成功保存后只清理已吸收作用域的备忘。
5. 外部素材按作用域分批：普通批次最多 8 条、估算 2000 token；合法大单条可独占一批，单条含调度开销最多 2600 token。
6. 模型可以返回 `【无日记】`。没有显式交代时，不写正式正文，只向 `provenance.no_write` 追加内部记录。
7. 命中显式交代或纠正时，即使模型漏写或返回“不写”，确定性通道也必须补齐原话，并将结果转为可保存正文。
8. 完整篇缺少具体锚点时允许重试一次；第二次仍缺锚点则降级为简记。显式交代补齐优先于该降级结果。
9. 自动写路径不允许用“拟人日常随笔”替代无新素材判断；无素材时不写，并按路径静默或明确提示。

完整日记的 575～1035 字、交接信的 600～900 字都是生成建议或当前输出约束，不是历史正文必须满足的读取条件。

## 3.4 历史落盘与原子性

一次日记保存必须依次满足：

1. 检查白名单与主密钥可用性；无法签名则拒绝写入。
2. 读取并验证已有日记条目；任何条目缺签、签名不匹配或内容损坏，都不得通过整包重写把它静默洗掉。
3. 若完整篇吸收简记，只修改对应分节头，追加“已吸收”。
4. 追加新正文与分节头，保留既有正文、图片和侧车。
5. 若本次同时更新 `provenance` 或外部素材决定，它们与日记正文在同一次 ZIP 原子重写中落盘。
6. 在内存中生成 ZIP 并执行 CRC 自检，再写唯一临时文件，回读并解密/解析自检，最后通过 `os.replace` 替换目标。
7. 任一步失败都不得把半成品当作有效版本；原 ZIP 损坏时可以隔离备份，但不得直接覆盖。

`current_self`、批注和其他独立侧车各有自己的读—验—改—签流程。整份插件状态不处于一个全局数据库事务中，不能宣称跨所有文件的全局原子提交。

## 3.5 快照整合

成功保存非 `life` 日记后，若 `current_self_enabled=true`：

1. 读取并验证现有快照；损坏时停止更新，不覆盖。
2. `since_rewrite + 1` 达到配置周期（默认每 4 次）、模型给出 `<rewrite_self>true</rewrite_self>`、或当前快照不存在时，执行整篇重写；否则只重写可替换段。
3. 整篇重写可读取按新到旧、估算最多 4000 token 的近期日记；滚段输出预算 300 token，整篇输出预算 1200 token。
4. 新正文最多保存 900 字。整篇重写可以新增带来源的锚点；滚段不得生成或修改锚点。
5. 保留用户确认事项；旧正文进入最多 5 版的 `history`。
6. 只有成功落盘后才将整篇重写计数复位；失败时保留上一有效快照。
7. `life` 随笔不触发快照更新；功能关闭时不读写、不更新、不自动注入快照。

用户确认事实级批注后，确认摘要追加到 `pending_items`，不直接改写快照正文或历史日记。批注已保存而确认事项追加失败时，必须记录失败，不能谎称两部分已在同一事务完成。

## 3.6 冲突消解

### 3.6.1 语义冲突

| 冲突 | 消解规则 |
| --- | --- |
| 旧记录与当前状态不同 | 旧记录保留为“当时如此”；当前快照更新为“现在如此”，不得回写旧正文。 |
| 新信息纠正用户事实 | 用户原话进入显式通道；事实级反思须经用户确认后才能成为有效确认事项。 |
| 锚点被证明错误或失效 | 给锚点追加 `superseded_by`，默认注入排除；不删除原锚点。 |
| 完整篇与既有简记重复 | 完整篇吸收同场景简记，旧简记标记“已吸收”；默认注入跳过旧简记，原文仍可回查。 |
| 模型输出与用户明确交代冲突 | 显式交代原话优先保留；模型不得用自主取舍覆盖用户明确纠正。 |
| 不同来源对同一事件说法不同 | 保留各自来源与原文，不因检索排名高就自动认定其为真。 |

AuSM 没有一个可以自动证明所有自然语言矛盾的真值求解器；语义冲突仍可能需要用户判断。

### 3.6.2 写入冲突

- 当前实例内，手动、哨兵和定时日记写作使用按 `sender_id` 的 `_writing` 互斥，因为同一用户的私聊与群聊写同一 ZIP。
- 哨兵在异步调度前先占用互斥并更新代际状态，避免同一实例重复调度。
- 原子替换防止读者看到半写 ZIP，但不等同于跨进程乐观锁或分布式事务。
- 当前规范不支持多个插件进程共享同一数据目录并发写入，也不提供多主合并、CRDT 或跨设备自动同步。
- 侧车独立更新与后台批注可能形成先后顺序；需要跨对象一致性的写入必须显式放在同一次 ZIP 重写中，不能从“单文件原子”推导出“全局无冲突”。

## 3.7 日记交接

新上下文第一轮正常 LLM 请求触发默认交接：

1. 私聊优先组装 `current_self` 正文，再按预算携带最新确认事项和最新有效锚点；群聊不自动注入 `current_self`。
2. 选择用户配置或管理员默认范围内的日记，默认最近 7 天；验签失败条目跳过并标注。
3. 跳过已吸收简记，按当前作用域过滤片段；具体群只读当前群与显式单向共享来源，私聊片段不得借道进入群聊。
4. 自动路径使用原文轻量节选，优先保留首尾及原话、时间、事件锚点，不额外调用模型摘要。
5. 默认总预算为 8000 字符且估算 2400 token；快照子预算最多 1200 token，锚点子预算最多 400 token。新日记优先，最终按旧到新顺序交付。
6. 预算不足时减少确认事项、锚点或历史节选；不得因此截断持久存储。
7. 默认临时模式只让注入参与新会话第一条回复，不写入会话历史；开启持久注入后可每轮放入 system prompt，代价是每轮消耗 token。
8. 临时模式只有在 LLM 响应成功后才确认本代际已交付；失败时允许后续重试。
9. 手动 `/注入日记` 保留所选篇目全文通道，并在本会话内去重；`/查看日记` 是用户阅读通道，不等于自动交接。

批注只在私聊、功能开启且本轮有可读日记时生成；生成失败不得阻断日记交接。默认注入只选 `open/deferred` 批注，`pending_confirmation` 与 `rejected` 不得作为有效当前认识混入。

## 3.8 搜索、批注与反思

- 搜索先验签并应用与注入相同的来源和作用域规则，再建立或查询派生索引。
- 当前索引为按 sender 隔离的内存 SQLite FTS；FTS5 trigram 不可用时使用确定性 n-gram，索引异常时回退规范化关键词 AND 线性搜索。
- BM25 只排序候选，日期只做同分辅助，不做时间衰减。
- 可选 LLM 查询扩展与候选重排默认关闭；模型只能选择已有证据，不得生成新证据。
- 无命中只表示本次查询与过滤条件未找到结果，不证明没有相关记忆。
- 批注锚点必须真实出现在本轮读过的可锚文本中；AI 私人随笔和 `<self>` 片段先替换为占位，不得成为事实锚点。
- 普通观察可以保留为批注；事实级写回必须由用户确认、否决或悬置。被否决记录保留但不参与有效注入。

## 3.9 外部素材生命周期

```text
pending ──吸收──> absorbed
   │  └──暂缓──> deferred ──吸收/暂缓/丢弃──> absorbed/deferred/discarded
   ├────明确丢弃──────────────> discarded
   └────超过保留期────────────> expired（text 清空）
```

- 模型漏给某条决定时，该素材保持原状态，不得默认吸收或丢弃。
- `absorbed_entry` 只在成功吸收时写入对应日记条目。
- 删除含外部写入的日记时，先把外部幂等记录转为 ZIP 外墓碑，再清理对应来源记录；墓碑损坏时后续写入失败关闭。
- 过期清理只针对超过配置保留期的 `pending/deferred`，保留期为 0 时关闭过期清理。

## 3.10 导出、导入与删除

导出时必须复制完整日记 ZIP 及其中已存在的日记、图片、批注、来源、快照、锚点归档与外部素材队列，并附该用户派生钥匙和清单。导出过程中不生成一份“只含当前注入内容”的缩水包。

导入必须先完整验证再安放；已有目标文件时拒绝覆盖。迁移语义无损的验证对象是：

- 日记正文与分节头的文本；
- 图片二进制与条目名；
- §2.6 中已存在 JSON 对象的规范化内容；
- 对象之间的来源引用关系。

允许变化的是签名、钥匙派生结果、ZIP 压缩字节、导出时间、临时文件名和说明文本。缺失侧车不得被导入器凭空补成有内容的版本。

用户明确删除日记时，可以级联删除对应图片和批注，并清理来源记录；外部写入先保留墓碑。删除是用户主权行为，不属于整合静默损失，但不可据此宣称“所有删除都可恢复”。

---

# 第四部分　主张与边界

## 4.1 AuSM 能保证什么

以下保证都受各自前提限制，不外推为泛化的“永不丢失”。

### G1　可读与可导出

**保证**：规范列明的日记及侧车不依赖向量数据库或私有模型才能读取；单用户迁移包包含完整日记 ZIP 和验证、迁移所需的用户派生钥匙。  
**前提**：数据未损坏、钥匙可用、导出流程成功；排除项见 §2.9。  
**验证**：解包后检查条目集合，独立解析 Markdown、JSON 与图片，并按签名公式验证。

### G2　限定对象集合的迁移语义无损

**保证**：一次成功的导出—导入往返保持 §3.10 所列规范化内容与引用关系，不要求 ZIP 逐字节相同。  
**前提**：完整验证通过、目标不存在覆盖冲突、同一规范语义范围内导入。  
**不覆盖**：对话到日记的摘要损失、快照更新损失、ZIP 外墓碑、偏好、备忘、凭证与运行状态。  
**验证**：导出前后规范化对象做结构等值比较，图片做字节等值比较，签名允许重签差异。

### G3　整合不静默改写历史正文

**保证**：日常整合可以追加新片段或给简记分节头加“已吸收”，但不得用当前理解替换旧日记正文。  
**例外**：用户明确删除、受控旧格式迁移、规范公开的元数据容量淘汰。  
**验证**：整合前后旧片段正文一致；允许变化的只有规范列明的分节头标记和新增内容。

### G4　完整性失败时不静默覆盖

**保证**：已纳入签名域的条目在缺签、签名不匹配或无法解析时，不作为可信内容读取，也不通过重写自动洗成有效签名。  
**不覆盖**：密钥已经泄露后的恶意重签、未纳入签名域的外部状态、内容本身的真实性。  
**验证**：修改正文或侧车一个字节后，读取与覆盖必须失败关闭。

### G5　交接预算与持久存储分离

**保证**：上下文预算不足只影响本次交接工作集，不直接删除历史正文或快照存储。  
**不覆盖**：明确容量淘汰、保留期、用户删除及快照自身的滚动重写。  
**验证**：构造超预算历史，比较注入前后 ZIP 权威内容。

### G6　原文为权威、索引可重建

**保证**：FTS、BM25 与可选模型辅助只负责定位或排序已有证据；索引不得生成权威正文。  
**验证**：删除或破坏派生索引后，可以从签名原文重建或回退线性搜索，原文不变。

## 4.2 AuSM 不保证什么

1. **不保证原始对话采集无损。** 历史截断、游标回退、模型取舍、“不写”和摘要都会损失信息。
2. **不保证快照完备。** 快照有长度上限并允许整篇重写，重要细节可能只剩在原文中。
3. **不保证事实正确。** 第一人称叙述、来源标签、用户相关分类与 HMAC 都不是事实真值证明。
4. **不保证语义召回或召回完备。** 词法检索可能漏掉同义表达、隐含指代和换言说法；无命中不能推出不存在。
5. **不保证模型路径确定性。** 日记、快照、批注、查询扩展和重排会随模型、参数、人设与上下文变化。
6. **不保证自动消解全部语义冲突。** 当前时间状态、事实确认和来源分歧仍可能需要用户裁决。
7. **不保证模型内部形成长期记忆。** 注入只是当前请求可见的外部上下文，不更新模型参数。
8. **不保证无限规模。** 单 sender ZIP 整包重写、文档收集和索引构建会随历史增长产生存储、内存、I/O 与延迟成本。
9. **不保证任意并发与多主同步。** 当前没有跨进程事务、分布式锁、CRDT 或自动冲突合并。
10. **不保证全部元数据永久保留。** 批注、外部幂等记录和墓碑存在容量淘汰，快照正文历史只保留最近 5 版。
11. **不保证跨设备删除重放边界完全相同。** ZIP 外墓碑不随单用户迁移包迁移。
12. **不保证绝对安全。** HMAC 不是加密；可选静态加密也有口令丢失、配置同泄露和运行中明文等边界。
13. **不保证已有长期质量优于其他路线。** 当前规范不是对比实验结论，不能由功能清单或单元测试数量推出体验优势。

## 4.3 当前实现的明确限制与未测量包线

| 项目 | 当前限制或状态 |
| --- | --- |
| 快照正文 | 最多 900 字；正文历史最多 5 版 |
| 活动锚点 | 最多 200 条；溢出进入独立归档 |
| 锚点归档 | 没有总条数硬上限 |
| 快照整篇输入 | 近期日记按新到旧，估算最多 4000 token |
| 默认交接 | 8000 字符且估算 2400 token；快照最多 1200 token，锚点最多 400 token |
| 批注 | 最多 500 条，超限按公开规则淘汰 |
| 每日 `self` 来源 | 最多 50 条、合计 2000 字 |
| 每日显式交代来源 | 独立最多 50 条、合计 2000 字 |
| 外部幂等记录 | 每日最多 100 条，全 ZIP 最多 2000 条 |
| 删除墓碑 | 最多 2000 条 |
| 活动外部素材 | `pending + deferred` 最多 100 条 |
| 外部素材批次 | 通常最多 8 条、2000 token；单条含开销最多 2600 token |
| 素材游标 | 最多 64 个场景 |
| 单张图片 | 最多 8 MiB |
| 单 sender 图片总量 | 最多 256 MiB |
| 单用户迁移包上传 | 压缩本体最多 32 MiB |
| 迁移包解压检查 | 最多 10000 条目；单条目和总解压量各最多 64 MiB；压缩比规则见 §2.9 |
| `no_write` 内部记录 | 预留清理字段，但当前没有总量硬上限或自动清理承诺 |
| 历史日记总量、最大可用年限 | 未建立实测上限 |
| 最大并发、P95/P99 延迟、内存峰值 | 未建立公开测量结果 |
| 长期召回率、关系连续性盲评 | 未由本规范提供实验结论 |

“没有设定硬上限”不等于“支持无限规模”；“尚未测量”也不得被写成“性能良好”。

## 4.4 什么时候不要选 AuSM

| 场景 | 不匹配原因 | 更合适的路线 |
| --- | --- | --- |
| 高并发、多写者、强事务业务状态 | AuSM 当前 Profile 不提供跨进程事务与多主合并 | 数据库、显式状态机与并发控制 |
| 大规模语义知识库问答 | 路线重点是自传与关系连续性，不是语义检索覆盖率 | 向量或混合 RAG、领域知识库与评估集 |
| 不允许模型改写或归纳 | 日记和快照整合天然包含生成式取舍 | 规则化显式记忆、结构化字段或人工审核写入 |
| 合规审计要求保留全部原始事件 | AuSM 不是全量事件日志，摘要和容量淘汰不满足该要求 | 追加式事件日志、不可变审计存储；AuSM 只能作为派生视图 |
| 只保存短期任务状态 | 日记生成、快照维护和原文管理会带来不必要成本 | 会话状态、任务队列或轻量键值存储 |
| 要求精确、可重复的事实计算 | 自然语言快照不是权威计算状态 | 权威数据库与确定性计算，必要时把结果摘要交给 AuSM |
| 需要自动跨设备多主同步 | 当前没有同步协议和冲突合并语义 | 具有明确同步与冲突策略的存储系统 |
| 无法承担模型调用成本或数据外发 | 写作与快照整合需要模型；可选检索辅助还可能外发查询 | 本地规则、手动日记或完全离线且确定性的记忆方案 |
| 只追求最高搜索命中率 | AuSM 的价值假设不能被单一检索指标代表，也不承诺最优召回 | 以检索评估为目标优化的专用记忆/搜索系统 |

## 4.5 作为技术路线的证据边界

本规范公开的是设计、数据契约与当前实现边界。它可以作为后续技术论文的方法与系统实现基础，但不替代：

- 与结构化画像、摘要记忆、向量 RAG 等路线的同任务对照；
- 对历史定位、事实保持、快照遗漏、token 成本和用户连续性感知的分别测量；
- 跨模型、跨时间、跨规模的真实运行评估；
- 对写入冲突、迁移往返、损坏恢复和隐私边界的持续回归。

在这些证据补齐之前，适当表述是“AuSM 提出并实现了一种以自传原文、当前快照和证据回源组成的上下文连续性路线”，而不是“AuSM 已被证明是更优的通用长期记忆方案”。

## 4.6 v0.1 的变更纪律

- 修改 Core 不变量、字段语义、签名域或迁移等值规则，必须提升相应规范或 Profile 版本，并写明迁移方式。
- 新增可选字段时应保持旧读取器可识别；改变已有字段含义不得伪装成“兼容新增”。
- 调整实现参数只需更新 Profile 与对应文档；不得反向把新参数写成所有 AuSM 实现的必要条件。
- 代码与本规范冲突时，必须公开记录并修正其中一方，不得静默选择对宣传更有利的口径。
- 规范中的每条“保证”都应对应可重复的验证方法；无法验证或尚未测量的内容一律留在边界，不留宣传性例外。

---

**一句话边界**：AuSM 保存的是可回源的自传历史与可更新的当前理解；它能让上下文交接更有连续性，但不把生成式整理伪装成全量记忆、事实真值或无限扩展的存储系统。
