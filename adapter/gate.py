# -*- coding: utf-8 -*-
"""指令入口闸门：多 AI 防冲突、白名单拒绝提示、指令参数解析。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

import re

from astrbot.api.event import AstrMessageEvent

from ..memory_diary.common import strip_trailing_decorations
from ..memory_diary.log import logger
from .compat import At


class GateMixin:
    """指令入口闸门：多 AI 防冲突、白名单拒绝提示、指令参数解析。"""

    # ------------------------- 群聊多 AI 防冲突 -------------------------

    def _should_respond(self, event: AstrMessageEvent) -> bool:
        """判断这条指令是不是发给我的（群里多个 AI 都装了本插件时防冲突）。

        - 私聊：永远响应；
        - 群里 @了我（QQ 原生 At 组件指向我）：响应；
        - 群里明确 @了别人（At 组件不是我）：沉默，让被点名的那个 AI 响应；
        - 指令 token 内出现任何文本形式的 @（如 "/写日记@小深"）：一律视为
          在叫别人、保持沉默——QQ 原生 At 组件带 QQ 号已足够可靠，
          文本 @ 无法确认身份，宁可沉默也不抢话；
        - 群里没有任何 @：响应（广义兼容模式），除非配置开启"群里只响应@我的指令"。
        """
        if not self._is_allowed(event.get_sender_id()):
            # 白名单外的用户：静默拒绝（不回复、不刷屏），只留 debug 日志
            logger.debug(
                f"[memory_diary] 用户 {event.get_sender_id()} 不在白名单，已静默忽略指令"
            )
            return False
        if not event.get_group_id():
            return True
        self_id = ""
        try:
            self_id = str(event.get_self_id())
        except Exception:
            pass
        at_me, at_other = False, False
        if At is not None:
            try:
                for comp in event.get_messages():
                    if isinstance(comp, At):
                        if self_id and str(comp.qq) == self_id:
                            at_me = True
                        else:
                            at_other = True
            except Exception:
                pass
        if at_me:
            return True
        # 文本形式的点名只看"指令本体"（第一个空白前的 token，如 "/写日记@小深"）：
        # 内容里出现的 @（例如 /记一下 记得@张三 他下周考试）不算点名，避免误判沉默
        full_text = (event.message_str or "").strip()
        cmd_token = full_text.split(None, 1)[0] if full_text else ""
        if re.search(r"@[^\s@，,]+", cmd_token):
            # 指令 token 内出现任何文本 @ 一律视为在叫别人，保持沉默
            return False
        if at_other:
            return False
        return not bool(self.config.get("group_require_at", False))

    # ------------------------- 白名单拒绝提示（设计方案附录 A 修复） -------------------------
    #
    # v0.5.5 发现的问题：白名单静默拦截时，AstrBot WebUI 内置测试聊天的 sender_id
    # 不是用户 QQ 号，只配置了 QQ 号的用户发 /删除日记 会「完全没反应」——分不清是
    # 不在白名单还是插件坏了。修复：私聊里对**指令形态**的消息回一次性提示（带 UID，
    # 指引配置页加白名单）；群聊保持静默（不暴露插件、不抢话）；普通闲聊不提示；
    # 名单内用户完全无感。每个身份（取不到身份时按会话）只提示一次，防刷屏。
    _WHITELIST_HINT_TEXT = (
        "📖 这条日记指令没有执行：你的身份（UID）「{sid}」不在本插件的用户白名单里。\n"
        "如果这是你自己（或 AstrBot WebUI 的内置测试聊天）：请管理员打开插件配置页，"
        "把「允许使用插件的用户」填上这个 UID（多个号用逗号或空格分隔），"
        "保存并重载插件后再试。\n"
        "（本提示每个身份只发一次；群聊里不会提示，普通聊天也不会提示。）"
    )
    _WHITELIST_HINT_TEXT_NO_ID = (
        "📖 这条日记指令没有执行：插件取不到你的身份 UID"
        "（可能来自 AstrBot WebUI 的内置测试聊天），而本插件只允许白名单里的用户使用。\n"
        "如果这是你自己：请管理员打开插件配置页的「允许使用插件的用户」，"
        "把插件日志里这条消息的 sender id 加进去（或临时清空白名单验证），"
        "保存并重载插件后再试。\n"
        "（本提示每个会话只发一次；群聊里不会提示，普通聊天也不会提示。）"
    )

    def _is_command_shaped(self, text: str) -> bool:
        """这条消息是不是日记插件指令（带前缀的，或裸指令词）。"""
        try:
            return bool(
                self._is_op_command(self._strip_invisible(text or "").strip())
            )
        except Exception:
            return False

    async def _maybe_hint_whitelist_denied(self, event: AstrMessageEvent) -> None:
        """白名单拒绝时，给私聊里的指令形态消息发一次性提示（附录 A 修复）。"""
        try:
            if event.get_group_id():
                return  # 群聊保持静默（附录 A 明确要求）
            sender_id = str(event.get_sender_id() or "").strip()
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            key = sender_id or f"umo:{umo}"
            if key in self._whitelist_hint_sent:
                return  # 一次性：同一身份/会话不再提示
            if self._is_allowed(sender_id):
                return  # 名单内用户完全无感
            if not self._is_command_shaped(event.message_str or ""):
                return  # 普通闲聊不提示（不打扰、不暴露插件）
            self._whitelist_hint_sent.add(key)
            self._cap_state(self._whitelist_hint_sent)
            if sender_id:
                text = self._WHITELIST_HINT_TEXT.format(sid=sender_id)
            else:
                text = self._WHITELIST_HINT_TEXT_NO_ID
            logger.info(
                f"[memory_diary] 白名单外用户（sender={sender_id!r}）发来指令，"
                "已发送一次性配置提示"
            )
            await event.send(event.plain_result(text))
        except Exception as e:  # 提示失败绝不能影响消息处理
            logger.warning(f"[memory_diary] 白名单拒绝提示发送失败: {e}")

    # 零宽与方向控制字符：其他插件会在 message_str 末尾追加不可见标记，
    # 不剥掉会把指令参数判为"不认识"（日志里用 repr 才能看到它们）
    _INVISIBLE_RE = re.compile(
        "[\u200b-\u200f\u202a-\u202e\u2060\ufeff]"
    )

    @classmethod
    def _strip_invisible(cls, text: str) -> str:
        """剥离零宽/方向控制字符与尾部 ``[MSG_ID:…]`` 机器标记（只用于解析）。

        P2-11 修复：qq_tools（show_message_id）等插件会往消息尾部追加
        ``[MSG_ID:-2037476869]`` 这类机器标记。以前只剥零宽字符，带参指令
        （``/日记 完整``、``/日记范围 最近7天``、``/日记密码``…）参数里带着标记，
        被当成"无法识别参数"。这里与 ``/reset`` 判定共用同一套口径
        （``memory_diary.common.strip_trailing_decorations``：只剥尾部、循环剥完），
        解析链上所有调用点（含交互式追问的序号/确认解析）自动受益。
        纪律不变：只影响指令解析判断，绝不回写 event.message_str 原文。
        """
        cleaned = cls._INVISIBLE_RE.sub("", text or "")
        return strip_trailing_decorations(cleaned)

    def _command_arg(self, event: AstrMessageEvent) -> str:
        """从原始消息取指令后的完整参数，并剥掉"真正的" @提及。

        P2-3 修复：AstrBot 的 CommandFilter 按空格拆参数，只把第一个 token 传给
        处理函数的参数——"/日记范围 2026-10-01 到 2026-10-23" 会被截成单日。
        这里直接解析 event.message_str，保留带空格的完整参数。

        P3-5 修复：以前无条件用正则剥掉参数里所有 ``@…``，
        ``/形象 修改 黑发@红色发带`` 会被截成"修改 黑发"。现在只剥两类**真正的**
        提及：(1) 消息链里 At 组件对应的 ``@<QQ号>`` 文本形式；(2) 参数开头的
        独立 @提及（``/注入日记 @我的助手 最新`` 这种）。正文中间的 ``@`` 原样保留。
        """
        text = self._strip_invisible(event.message_str or "").strip()
        parts = text.split(maxsplit=1)
        arg = parts[1] if len(parts) > 1 else ""
        # 全角空格（U+3000）无需额外兜底：str.split() 默认按 Unicode 空白切分，
        # 已把全角空格当分隔符处理。
        return self._strip_arg_mentions(event, arg).strip()

    def _strip_arg_mentions(self, event: AstrMessageEvent, arg: str) -> str:
        """只剥真正的 @提及，保留正文里的 @（P3-5）。"""
        if not arg or "@" not in arg:
            return arg
        # 1) 消息链里真有 At 组件时，剥掉其 QQ 号的文本形式（@12345678）。
        qqs = set()
        if At is not None:
            try:
                for comp in event.get_messages() or []:
                    if isinstance(comp, At):
                        qq = str(getattr(comp, "qq", "") or "").strip()
                        if qq:
                            qqs.add(qq)
            except Exception:
                pass
        out = arg
        for qq in sorted(qqs, key=len, reverse=True):
            out = out.replace(f"@{qq}", "")
        # 2) 参数开头的独立 @提及（@昵称 后面跟参数）：真提及通常独立成词放在最前，
        #    正文中间的 @（如"黑发@红色发带"）不算提及、必须保留。
        out = re.sub(r"^@[^\s@，,]+\s*", "", out)
        return out
