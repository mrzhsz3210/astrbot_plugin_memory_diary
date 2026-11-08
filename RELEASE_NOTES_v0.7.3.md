# 记忆日记 v0.7.3 发布说明（开发稿）

## 陪伴插件外部记忆 Provider

- 接入 `astrbot_plugin_private_companion` 7.0.7 起提供的 `companion.external-memory.v1`；建议联调使用 7.1.6，AstrBot 需 4.27.2+。
- 单注册 Provider ID 为 `astrbot_plugin_memory_diary.memory`，priority=10，默认回调预算 5 秒；`handles_model_memory` 保持默认 `False`。
- `read`：仅在身份可解析、白名单允许的已授权私聊中返回最多 3 条验签日记原文片段，遵守请求的字符预算；群聊和无法识别身份的请求不返回资料。
- `write`：接收陪伴插件已保存的日记/梦境/主动消息**存证副本**，以 `source_plugin + memory_id` 幂等更新，按 installation、Bot、角色与绑定修订隔离。
- `summary`：只统计当前请求身份的日记篇数，归入固定六类中的 `other` 与 `total`；身份或读取失败时明确报错，不伪造零计数。
- Provider 生效时仅让出私聊常规自动注入；群聊、手动 `/注入日记`、按需历史召回与上下文哨兵保持原行为。

## 存证、权限与数据边界

- 存证位于 `data/plugin_data/astrbot_plugin_memory_diary/companion_evidence/<role_key_hash>/YYYY-MM-DD.jsonl`，配套 `.jsonl.sig` 使用 `.diary_key` 做 HMAC。
- 这里的签名只证明“记忆日记于 `received_at` 收到该副本”，不证明内容为真，也不表示本插件为第三方内容背书。
- 存证固定保留 90 天，插件启动时删除过期整天文件；验签失败拒绝读取和覆盖。存证不参与 read、不进入写日记素材、不在日记本 Web 展示；唯一用户出口是私聊 `/日记状态` 的条数与角色数。
- 陪伴插件仍保存其原始日记和主动消息；本功能接收的是副本。read 返回的日记原文会进入陪伴插件的外部记忆提示词管线，管理员应向用户说明这一处理范围。
- `companion_write_evidence=auto` 在 MemoryCompanion 活跃时不注册 write；`off` 不注册；`on` 强制注册。v1 是单 writer：注册 write 后 MemoryCompanion 旧桥不再收到这些推送，这是宿主设计，不是故障。
- 不新增公开 HTTP API，不导入陪伴插件或 MemoryCompanion 的代码；接口缺失、版本不符或注册失败时原功能不受影响。

## 备份、迁移与删除

- 存证不随日记迁移包、单用户导出或日记一键备份走；随 AstrBot 数据目录整体备份覆盖。
- 整体备份必须同时保存 `.diary_key`。密钥丢失后存证无法验签，等同作废，没有恢复后门。
- 停用 Provider 只停止新协作，不自动删除历史存证。要彻底删除，请在停用插件后删除 `companion_evidence/`；若卸载流程保留数据目录，也需手动删除该目录或整个插件数据目录。

## 已知限制与回退

- 对方注入的时序和体积不完全受本插件控制，上下文哨兵可能低估占用；本版不调整哨兵算法。
- 回退到 v0.7.2 前，将三个陪伴兼容配置关闭；旧版不认识新增存证目录。旧版不会读取或迁移这些记录，如需彻底移除请按上文手动删除。
- 发布前仍需在陪伴插件 7.1.6 真机上完成接口签名确认与六步联调；开发期验证全部使用 mock，不访问运行中的 AstrBot 实例。
