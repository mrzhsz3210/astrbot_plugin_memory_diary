# -*- coding: utf-8 -*-
"""核心基类：组合存储与服务，管理插件级内存状态与生命周期。

这里不出现任何 `astrbot` import；AstrBot 相关的指令装饰器、事件钩子、
Web API 与主动消息发送留在 main.py。
"""

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import strip_invisible
from .constants import PLUGIN_NAME, _STATE_CAP
from .log import logger
from .service import ServiceMixin
from .storage import StorageMixin


class MemoryDiaryCore(StorageMixin, ServiceMixin):
    """插件业务核心，可被 main.py 的 Star 子类直接继承。"""

    README_ZIP_ENTRY = "说明.txt"
    ENTRY_NAME_RE = re.compile(r"^diaries/\d{8}\.md$")  # 当前格式
    _LEGACY_DAY_RE = re.compile(r"^\d{8}\.md$")  # v3.1~3.3 平铺按天，仅迁移用
    _V3_ENTRY_RE = re.compile(r"^\d{8}_\d{6}(_\d+)?\.md$")  # v3.0 按篇命名，仅迁移用

    # 日记片段模式：完整 / 简记 / 随笔。旧格式没有模式标记：
    # 含"补记"视为简记，含"无新对话"视为随笔，其余视为完整。
    _MODE_LABELS = {"full": "完整", "brief": "简记", "life": "随笔"}

    _MEMO_LIMIT = 20  # 每个场景的备忘上限

    _DATE_PAT = r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?"

    # 日记片段分隔条（如 "—— 当天 20:30 补记 · 写于群聊 ——"、"—— 14:05 · 写于私聊 ——"）
    _SEG_HEADER_RE = re.compile(r"^—— .+ ——$")

    # 需要从写作素材中剔除的日记操作：用户侧的指令、AI 侧的插件固定通知前缀
    _OP_USER_COMMANDS = (
        "日记",
        "写日记",
        "查看日记",
        "删除日记",
        "日记状态",
        "日记范围",
        "日记帮助",
        "注入日记",
        "记一下",
        "记住",
        "查看备忘",
        "删除备忘",
        "diary",
        "diary_view",
        "diary_delete",
        "diary_status",
        "diary_scope",
        "diary_help",
        "diary_inject",
        "memo",
        "memo_view",
        "memo_delete",
        "reset",
        "重置",
    )
    _OP_ASSISTANT_PREFIXES = ("📖", "⚠️ 上下文已用", "📝 小提醒", "📌 已记下")

    # ---- 查看密码（仅挡 Web 日记本页面；服务器文件权限不在保护范围内） ----
    _PW_ALGO = "pbkdf2-sha256"
    _PW_ITER = 100_000

    def __init__(self, context, config=None, data_dir=None):
        super().__init__(context, config)
        self.context = context
        self.config = config or {}

        # 数据目录：<AstrBot>/data/plugin_data/astrbot_plugin_memory_diary/
        # 由 main.py 通过 StarTools 解析后传入；单独使用时回退相对路径。
        self.data_dir = Path(data_dir) if data_dir is not None else (
            Path("data/plugin_data") / PLUGIN_NAME
        )
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(
                f"[memory_diary] 数据目录创建失败（{self.data_dir}）：{e}"
                "——请检查 AstrBot 数据目录的写入权限，插件将无法保存日记"
            )

        # 记录每个会话（umo）上一次注入日记时的 conversation_id，避免重复注入
        self._injected_cid: dict = {}
        # 每个会话已注入过的日记篇目：umo -> {"cid": str, "names": set}
        self._injected_entries: dict = {}
        # 用户主动要求的追加注入（/注入日记）：umo -> scope，下一次请求时生效
        self._pending_inject: dict = {}
        # 上下文哨兵状态：umo -> {"cid": str, "reminded": bool, "auto_written": bool}
        self._watch_state: dict = {}
        # 防止同一会话并发触发自动写日记
        self._writing: set = set()
        # 日记操作冷却：sender_id -> 上次写日记/删日记的时间
        self._op_last: dict = {}
        # 签名密钥内存缓存（None 表示尚未加载；加载失败保持 None 并拒绝签名操作）
        self._key_cache = None
        # reset 双保险：见到 /reset 指令文本时记入，下一次 LLM 请求按新代际处理（P0-1 补强）
        self._reset_pending: set = set()
        # 无效时区只警告一次
        self._tz_warned = False
        # 阈值顺序被截断只警告一次
        self._ratio_warned = False
        # 旧开关 enable_context_watch 的迁移提示只记一次
        self._watch_migrate_warned = False

        # token 估算校准：umo -> {"key": provider::model, "estimate": int}
        # （on_llm_request 暂存本次静态估算，on_llm_response 拿真实用量后消费）
        self._pending_estimates: dict = {}
        # 最近一次请求时「哨兵口径」的上下文占用：umo -> {"used","limit","key"}
        # 供 /日记状态 与手动写日记门槛复用，保证三处显示同一个数（v2 估算下）
        self._last_context_used: dict = {}
        # 校准系数内存缓存（None 表示尚未从 token_calibration.json 加载）
        self._token_calib = None

        # 每天定时自动写完整日记：后台任务、私聊用户登记表、当天触发状态
        self._auto_task = None
        self._auto_users: dict = {}
        self._auto_state: dict = {}
        # 自动写日记时间格式错误只警告一次（记录上一次无效值）
        self._auto_time_warned = ""
        # 插件是否已进入终止流程：终止后即使还有旧 handler 被派发也不许再起后台任务
        self._terminated = False

    # ------------------------- 白名单 -------------------------

    def _allowed_user_ids(self) -> set:
        """解析配置的用户白名单；空配置返回空集（表示不限制，向后兼容）。

        P3-12：管理员从聊天里复制/粘贴的 QQ 号可能带零宽、方向控制字符或
        全角数字，解析前统一剥离不可见字符并做 NFKC 归一，否则 ID 精确匹配
        会把管理员自己挡在门外。
        """
        raw = strip_invisible(str(self.config.get("allowed_users", "") or ""))
        raw = unicodedata.normalize("NFKC", raw)
        return {t for t in re.split(r"[,，、\s]+", raw) if t}

    def _is_allowed(self, sender_id) -> bool:
        """白名单判定（纯逻辑、无 AstrBot 依赖）。

        - 名单为空 → 恒 True（老用户零感知）；
        - 名单非空且 sender_id 为空/不在名单 → False（fail closed，取不到身份一律不放行）；
        - ID 按字符串精确匹配。
        """
        allowed = self._allowed_user_ids()
        if not allowed:
            return True
        sid = str(sender_id or "").strip()
        if not sid:
            return False
        return sid in allowed

    async def initialize(self):
        """插件被激活时调用：载入自动写日记的私聊用户表，启动后台定时任务。"""
        self._terminated = False
        try:
            self._load_auto_users()
            self._auto_state = self._load_auto_state()
        except Exception as e:
            logger.warning(f"[memory_diary] 初始化自动写日记状态失败: {e}")
            self._auto_users = {}
            self._auto_state = {}
        try:
            # 用 AstrBot 已有的私聊会话补齐用户表：升级后不必等每位用户先发消息
            await self._seed_auto_users_from_conversations()
        except Exception as e:
            logger.warning(f"[memory_diary] 从会话库补齐自动写日记用户失败: {e}")
        self._start_auto_diary_task()

    # ------------------------- 时间 -------------------------

    def _now(self):
        """按配置的时区取当前时间；时区未配置或无效时回退服务器本地时间。"""
        tz_name = str(self.config.get("diary_timezone", "") or "").strip()
        if tz_name:
            try:
                return datetime.now(ZoneInfo(tz_name))
            except Exception:
                if not self._tz_warned:
                    self._tz_warned = True
                    logger.warning(
                        f"[memory_diary] 时区配置 {tz_name!r} 无效，回退服务器本地时间"
                    )
        return datetime.now()

    @staticmethod
    def _cap_state(d):
        """内存状态上限（dict 或 set）：超出容量时丢弃最旧的条目。"""
        if isinstance(d, dict):
            while len(d) > _STATE_CAP:
                d.pop(next(iter(d)))
        else:
            while len(d) > _STATE_CAP:
                d.pop()

    @staticmethod
    def _is_cancel(text: str) -> bool:
        """会话等待中的取消口令：用户改主意时立即退出等待，不必干等超时
        （等待期间本会话消息都被 waiter 接管，没有取消口令体验很糟）。"""
        return text.strip().lower() in (
            "取消",
            "算了",
            "不用了",
            "退出",
            "cancel",
            "q",
            "quit",
        )

    @staticmethod
    def _scope_desc(scope) -> str:
        if not scope:
            return "管理员默认"
        if scope == "all":
            return "全部日记"
        if scope == "latest":
            return "最新一天"
        if scope.startswith("days:"):
            return f"最近 {scope[5:]} 天"
        if scope.startswith("range:") and len(scope) >= 23:
            s, e = scope[6:14], scope[15:23]
            return f"{s[:4]}-{s[4:6]}-{s[6:]} 到 {e[:4]}-{e[4:6]}-{e[6:]}"
        return "最新一天（默认）"

    async def terminate(self):
        import asyncio

        # 先标终止并停后台定时任务，避免插件卸载后继续写日记/发通知；
        # 置 _terminated 后即使还有旧 handler 被派发，也不会再新建任务（窄窗口防泄漏）。
        self._terminated = True
        task = getattr(self, "_auto_task", None)
        self._auto_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[memory_diary] 停止自动写日记任务异常: {e}")
        self._injected_cid.clear()
        self._injected_entries.clear()
        self._pending_inject.clear()
        self._watch_state.clear()
        self._writing.clear()
        self._op_last.clear()
        self._reset_pending.clear()
        self._pending_estimates.clear()
        self._last_context_used.clear()
