# 代码结构（解耦说明）

本插件原来是一个约 4900 行的 `main.py`。v0.5.0 的这次重构把它拆成
**AstrBot 适配层 + 可独立导入的领域核心**，对外行为、配置项、数据格式、
指令与 Web 页面均保持不变。

```text
main.py                         AstrBot 适配层（指令/事件钩子/Web 路由/消息发送）
└── memory_diary/               领域核心（不 import astrbot，可单独测试）
    ├── constants.py            提示词、注入模板、默认值
    ├── errors.py               DiaryKeyUnavailable / DiaryTamperedError / DiaryCorruptError
    ├── common.py               文件名 key、图片魔数嗅探等无状态工具
    ├── log.py                  日志兼容层（有 AstrBot 用其 logger，无则静默丢弃）
    ├── storage.py              文件布局、HMAC 验签、偏好/备忘、旧格式迁移、
    │                           迁移包导入导出、Web 数据、图片入 zip（v0.6）、
    │                           批注侧车 annotations.json（v0.6）
    ├── service.py              会话历史 → 写作素材 → LLM 写作 → 保存、
    │                           上下文哨兵、token 校准（EWMA）、每天定时自动写日记
    ├── token_estimator.py      纯函数 token 估算：分类加权 + 消息/工具/多模态开销（v0.5.5）
    ├── imaging.py              生图通道：本地 SD-WebUI / ComfyUI、云端 OpenAI 兼容
    │                           images API、尺寸/超时/参考图退化、用量记账（v0.6 插图）
    ├── illustration.py         插图领域编排：AI 意愿×概率×限额闸门、正文标记解析、
    │                           用户照片扫描、PENDING 占位与异步落图、形象档案（v0.6 插图）
    ├── annotation.py           批注领域编排：reset 同步生成、注入裁剪/渲染、
    │                           /查看批注 文案、QQ 第五部分、写回蒸馏、Web 角标数据（v0.6 批注）
    └── core.py                 MemoryDiaryCore：组合上述 mixin，管理内存状态/生命周期
```

> v0.6 起 `image_*` / `annotation_*` 两个 feature 完全独立、各自可开关：
> 插图代码只出现在 `imaging.py` / `illustration.py`，批注代码只出现在
> `annotation.py`；两者共用 storage 的 zip / HMAC 公共能力，互不 import。**

## 为什么这样切

- **`storage.py` 只碰文件和密码学**：日记 zip、`.diary_key`、HMAC 派生钥匙、
  偏好 JSON、备忘、旧格式迁移、迁移包验签都在这一层；它不 import AstrBot。
- **`service.py` 只编排流程**：AstrBot 能力通过 duck-typed 的 `self.context`
  使用（`conversation_manager`、`persona_manager`、`get_using_provider_async`、
  `send_message`），不依赖具体事件/消息类型。
- **`main.py` 只做适配**：`@filter.command`、事件钩子、Web 路由、消息文件发送。
  装饰器处理函数保留在主类所在模块，避免 AstrBot 按 `main` 模块路径绑定
  handler 时找不到方法。
- **基类组合而不是复制**：`MemoryDiaryCore(StorageMixin, ServiceMixin)`，
  指令处理函数里的 `self._xxx()` 调用完全不变，重构不需要改动业务调用点。

## 兼容性约束

1. **handler 模块路径**：AstrBot 用 `data.plugins.<插件目录>.main` 作为模块路径，
   再按该路径绑定处理函数。因此所有带 `@filter.*` 的方法必须物理定义在
   `main.py` 中；核心逻辑可以下沉，但装饰器入口不要挪走。
2. **相对导入**：`main.py` 用 `from .memory_diary...`。插件必须按 AstrBot
   的包路径加载（`import data.plugins.<dir>.main`），不能直接
   `python main.py`。
3. **热重载**：AstrBot 重载插件时会清理 `data.plugins.<dir>.` 前缀的模块，
   新增的 `memory_diary.*` 子模块都在该前缀下，会被一并清理。
4. **无 AstrBot 环境**：`import memory_diary.core` 仍可成功（日志静默丢弃，
   不依赖标准库 logging），方便单元测试和复用。

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
