# 代码结构（解耦说明）

本插件原来是一个约 4900 行的 `main.py`。v0.5.0 的这次重构把它拆成
**AstrBot 适配层 + 可独立导入的领域核心**，对外行为、配置项、数据格式、
指令与 Web 页面均保持不变。

```text
main.py                         插件注册 + 组装 + @filter.* 入口薄壳（~200 行，不放业务逻辑）
├── adapter/                    AstrBot 适配层实现（可以 import astrbot），每个模块一个 mixin
│   ├── compat.py               AstrBot 可选能力的兼容导入（At/Image/TextPart/session_waiter/web…）
│   │                           + resolve_data_dir()
│   ├── lifecycle.py            启动清理：残留临时导出/备份包、插图临时文件
│   ├── gate.py                 指令闸门：多 AI 防冲突、白名单一次性提示、参数解析/不可见字符
│   ├── cmd_help_persona.py     /日记帮助、/形象
│   ├── cmd_memo.py             /记一下、/查看备忘、/删除备忘
│   ├── cmd_search.py           /回忆、memory_diary_search（继承 event 身份/作用域）
│   ├── cmd_write.py            /写日记
│   ├── cmd_view.py             /查看日记 + QQ 图文穿插发送
│   ├── cmd_annotation.py       /查看批注、/写回批注
│   ├── cmd_delete.py           /删除日记
│   ├── cmd_transfer.py         /导出日记、/导入日记
│   ├── cmd_settings.py         /日记密码、/日记状态、/日记范围、/注入日记
│   ├── hooks.py                on_any_message（/reset 识别）、on_llm_request（注入+哨兵）、
│   │                           on_llm_response（token 校准）
│   ├── web_diary.py            日记本 Web：路由注册、密码闸、读日记/插图、导入、批量删除
│   ├── web_export.py           日记本 Web：导出、一键备份、保存到本机下载目录
│   ├── web_search.py           时间轴/筛选/证据搜索；web_material.py：素材管理
│   ├── web_note.py             legacy /note；web_material.py：v2 /material
│   └── web_prefs.py            日记本 Web：界面偏好、自定义纸张/背景板
└── memory_diary/               领域核心（不 import astrbot，可单独测试）
    ├── constants.py            提示词、注入模板、默认值
    ├── errors.py               DiaryKeyUnavailable / DiaryTamperedError / DiaryCorruptError
    ├── common.py               文件名 key、图片魔数嗅探、尾部机器标记剥离等无状态工具
    ├── log.py                  日志兼容层（有 AstrBot 用其 logger，无则静默丢弃）
    ├── storage.py              文件布局、HMAC 验签、偏好/备忘、旧格式迁移、
    │                           迁移包导入导出、Web 数据、图片入 zip（v0.6）、
    │                           批注侧车 annotations.json（v0.6）
    ├── service.py              会话历史 → 写作素材 → LLM 写作 → 保存、
    │                           上下文哨兵后台任务、token 校准（EWMA）、每天定时自动写日记
    ├── writing.py              DiaryResult 解析、不写决策、锚点校验与显式记忆识别（v0.7）
    ├── current_self.py         交接信签名对象、滚段/整篇重写、活动锚点与原文归档
    ├── scope.py                MemoryScope、具体群隔离与单向共享（v0.7 收口）
    ├── search.py               统一证据检索、文档构建与线性回退
    ├── search_index.py         按 sender 隔离的内存 SQLite FTS、BM25、LRU 与失效
    ├── search_assist.py        严格 JSON 辅助边界与明确历史指代识别
    ├── external_material.py    签名素材队列、状态机、保留期与 AI 吸收契约
    ├── crypto.py               可选整 ZIP AES-256-GCM / Scrypt 静态加密与原子转换
    ├── token_estimator.py      纯函数 token 估算：分类加权 + 消息/工具/多模态开销（v0.5.5）
    ├── imaging.py              生图通道：本地 SD-WebUI / ComfyUI、云端 OpenAI 兼容
    │                           images API、尺寸/超时/参考图退化、用量记账（v0.6 插图）
    ├── illustration.py         插图领域编排：AI 意愿×概率×限额闸门、正文标记解析、
    │                           用户照片扫描、PENDING 占位与异步落图、形象档案（v0.6 插图）
    ├── annotation.py           批注领域编排：reset 同步生成、注入裁剪/渲染、
    │                           /查看批注 文案、QQ 第五部分、写回蒸馏、Web 角标数据（v0.6 批注）
    └── core.py                 MemoryDiaryCore：组合上述 mixin，管理内存状态/生命周期
```

类组合：`MemoryDiaryPlugin(*AdapterMixins, MemoryDiaryCore, Star)`。适配层 mixin
全部排在 `MemoryDiaryCore` 之前，覆盖关系与"方法都写在主类上"时完全一致。

> v0.6.1 之后（第二次解耦）：v0.5.0 拆出了 `memory_diary/`，但之后的功能迭代又把
> 指令/钩子/Web 实现堆回了 main.py（涨到 3400+ 行）。这次把它们按功能搬进
> `adapter/`，main.py 只剩入口薄壳。方法体逐字搬迁（AST 级逐一比对 79 个成员一致）。

## 新增功能时代码放哪（防止 main.py 再次变胖）

| 要加的东西 | 放哪 |
|---|---|
| 新指令 / 新事件钩子 | 实现写成 `adapter/<模块>.py` 里的 `async def _handle_<入口名>(...)`；main.py 只加装饰器 + 同签名薄壳 + 面向用户的 docstring |
| 新 Web 接口 | 实现放 `adapter/web_*.py`，在 `web_diary.py` 的 `_register_web_apis()` 里登记（Web 路由不靠装饰器，整个都不进 main.py） |
| 新的 AstrBot 可选 API | `adapter/compat.py` 里 try/except 导入，其他模块从 compat 取 |
| 与 AstrBot 无关的业务逻辑（存储、提示词、写作流程…） | `memory_diary/` 对应模块，保持不 import astrbot |
| 一个新的功能域 | 在 `adapter/` 新建模块 + mixin，加进 `adapter/__init__.py` 的 `AdapterMixins` |

薄壳模板（生成器型指令；普通协程型钩子用 `return await self._handle_xxx(...)`）：

```python
@filter.command("日记状态", alias={"diary_status"})
async def diary_status(self, event: AstrMessageEvent):
    """查看当前上下文占用、日记更新时间和哨兵状态。"""
    async for result in self._handle_diary_status(event):
        yield result
```

自检：main.py 里除了 `__init__` 不应出现 `if`/`try` 等业务分支；超过 ~250 行就说明有实现漏进来了。

> v0.6 起 `image_*` / `annotation_*` 两个 feature 完全独立、各自可开关：
> 插图代码只出现在 `imaging.py` / `illustration.py`，批注代码只出现在
> `annotation.py`；两者共用 storage 的 zip / HMAC 公共能力，互不 import。**

## 为什么这样切

- **`storage.py` 只碰文件和密码学**：日记 zip、`.diary_key`、HMAC 派生钥匙、
  偏好 JSON、备忘、旧格式迁移、迁移包验签都在这一层；它不 import AstrBot。
- **`service.py` 只编排流程**：AstrBot 能力通过 duck-typed 的 `self.context`
  使用（`conversation_manager`、`persona_manager`、`get_using_provider_async`、
  `send_message`），不依赖具体事件/消息类型。
- **`adapter/` 做 AstrBot 适配**：指令、事件钩子、Web 路由、消息/文件发送的实现。
  这一层可以 import astrbot，按功能分模块，每个模块一个 mixin。
- **`main.py` 只做注册与入口**：`@register`、类组合，以及带 `@filter.*` 的薄壳。
  装饰器入口必须留在 main.py：AstrBot 按 `handler.__module__` 绑定 handler
  （`StarHandlerMetadata.handler_module_path`），挪走就找不到方法。
- **基类组合而不是复制**：`MemoryDiaryCore(StorageMixin, ServiceMixin)`，
  指令处理函数里的 `self._xxx()` 调用完全不变，重构不需要改动业务调用点。

## 兼容性约束

1. **handler 模块路径**：AstrBot 用 `data.plugins.<插件目录>.main` 作为模块路径，
   再按该路径绑定处理函数。因此所有带 `@filter.*` 的方法（包括文件末尾动态套上
   `on_llm_response` 装饰器的那个）必须物理定义在 `main.py` 中；实现下沉到
   `adapter/`，装饰器入口不要挪走。
2. **相对导入**：`main.py` 用 `from .memory_diary...`。插件必须按 AstrBot
   的包路径加载（`import data.plugins.<dir>.main`），不能直接
   `python main.py`。
3. **热重载**：AstrBot 重载插件时会清理 `data.plugins.<dir>.` 前缀的模块，
   `memory_diary.*` 与 `adapter.*` 子模块都在该前缀下，会被一并清理。
4. **无 AstrBot 环境**：`import memory_diary.core` 仍可成功（日志静默丢弃，
   不依赖标准库 logging），方便单元测试和复用。
5. **发布打包**：`adapter/` 是运行时目录，发布包的文件清单需要包含它。

## 回归验证

重构没有改业务方法体：除 `__init__`、`_now`、`_scope_desc`、`terminate` 的
位置/装配调整外，其余方法体均由原文件原样切分。仓库的 `tests/` 下提供
**差分回归测试**：`baseline/`（重构前的原版）与当前实现同时加载，同一组
场景各跑一遍，比较磁盘状态与指令输出。不需要 pytest，在仓库根目录执行：

```powershell
# 需要一个能 import astrbot 的 Python 环境（Windows + Launcher 示例）
& "$env:USERPROFILE\.astrbot_launcher\instances\<实例UUID>\venv\Scripts\python.exe" tests\run.py

# 其他环境：用 ASTRBOT_CORE 指向 AstrBot 源码目录
$env:ASTRBOT_CORE = "D:\path\to\astrbot\core"; python tests\run.py
```

详见 `tests/README.md`（含覆盖清单与测试边界说明）。
