# -*- coding: utf-8 -*-
"""指令实现：/查看日记，以及 QQ 端图文穿插发送。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

import re
import secrets
from pathlib import Path

from astrbot.api.event import AstrMessageEvent

from ..memory_diary.log import logger
from .compat import _ImageComponent, SessionController, session_waiter, _SESSION_WAITER_OK


class ViewCommandsMixin:
    """指令实现：/查看日记，以及 QQ 端图文穿插发送。"""

    # ------------------------- 插图呈现（QQ 图文穿插） -------------------------
    #
    # v0.6：日记正文里的 markdown 图片引用（只认 images/ 开头，见契约第 2 节）由
    # illustration._split_images_for_send 切成 text/image/placeholder/warning 段，
    # 按顺序发多条消息，呈现"字 → 图 → 字"的翻日记本感觉（设计 7.1）。
    # 图片字节从用户日记 zip 里读（storage.load_image 负责验签），落一个临时文件
    # 交给 Image 组件，发完即删——数据目录里不留第二份图片副本。
    # 零图片时切段结果就是单段原文，输出与 v0.5.5 逐字节一致。

    async def _send_diary_image(self, event: AstrMessageEvent,
                                sender_id: str, name: str, alt: str) -> bool:
        """发送一张日记插图；成功返回 True，缺失/环境不支持时返回 False 由调用方降级。"""
        if _ImageComponent is None or not callable(
            getattr(_ImageComponent, "fromFileSystem", None)
        ):
            # 老版本 AstrBot 没有 Image.fromFileSystem：不崩插件，正文照常读
            logger.warning("[memory_diary] 当前 AstrBot 版本不支持图片消息，插图降级为占位文本")
            return False
        try:
            data, status = self.load_image(sender_id, name)
        except Exception as e:
            logger.warning(f"[memory_diary] 读取插图失败（{name}）: {e}")
            data, status = None, "missing"
        if not data:
            # missing 优雅降级（占位）；tampered 已在切段层转成 warning 段，
            # 这里若因并发变化读不到，同样按占位处理，不额外打扰用户。
            if status == "tampered":
                logger.warning(f"[memory_diary] 插图验签失败，按占位显示：{name}")
            return False
        suffix = Path(str(name or "")).suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            suffix = ".png"
        try:
            tmp_dir = self.data_dir / "temp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"[memory_diary] 创建插图临时目录失败: {e}")
            return False
        tmp = tmp_dir / f"{secrets.token_hex(16)}{suffix}"
        try:
            tmp.write_bytes(data)
            try:
                from astrbot.api.event import MessageChain

                chain = MessageChain(chain=[_ImageComponent.fromFileSystem(str(tmp))])
            except Exception as e:
                logger.warning(f"[memory_diary] 构造插图消息失败: {e}")
                return False
            await event.send(chain)
            return True
        except Exception as e:
            logger.warning(f"[memory_diary] 发送插图失败（{name}）: {e}")
            return False
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    async def _send_diary_content(self, event: AstrMessageEvent,
                                  sender_id: str, content: str):
        """发送日记正文：按插图引用切段做图文穿插，零图片时与旧版输出逐字节一致。

        调用方（/查看日记 的私有等待分支与"最新"回退分支）把
        ``f"📖 {label} 的日记：\\n\\n"`` 前缀并入 content 传给这里，
        前缀因此成为首段文本的一部分，不会被拆成单独一条消息。
        """
        segments = self._split_images_for_send(sender_id, content)
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "image":
                name = str(seg.get("name") or "")
                alt = str(seg.get("alt") or "")
                if await self._send_diary_image(event, sender_id, name, alt):
                    continue
                # 图片缺失/环境不支持：降级为占位文本（设计 6.3 优雅降级）
                if alt.startswith(self._USER_PHOTO_ALT_PREFIX):
                    fallback = f"[{alt}]"
                else:
                    fallback = f"[插图：{alt}]" if alt else "[插图]"
                await event.send(event.plain_result(fallback))
                continue
            text = str(seg.get("text") or "")
            if text:
                await event.send(event.plain_result(text))

    def _qq_annotation_section(self, sender_id: str, entry_name: str) -> str:
        """QQ 端某天日记的第五部分（页边批注）；异常时静默降级为空串。

        批注是附加内容：读取/验签出任何问题都只记 warning，绝不影响日记正文。
        零批注时返回 ""，调用方保持与 v0.5.5 逐字节一致的输出。
        """
        try:
            return (
                self.qq_annotation_section_text(sender_id, self._entry_day(entry_name))
                or ""
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 读取页边批注失败，已跳过第五部分: {e}")
            return ""

    # ------------------------- 指令：查看 / 删除 -------------------------

    async def _handle_view_diary(self, event: AstrMessageEvent):
        """查看日记（仅本人、仅私聊）：先列出时间，再按选择发送对应内容。（``main.view_diary`` 的实现）"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 日记是只给本人看的哦，请私聊我查看～",
                    "用户在群里想看日记，你要委婉地拒绝：日记是只给本人看的私密内容，请他来私聊再看。",
                )
            )
            return

        sender_id = event.get_sender_id()
        # 复审 12：密钥不可用时不要显示成"被篡改"——验签根本做不了，先说明
        if self._secret_key() is None:
            yield event.plain_result(
                "📖 签名密钥（.diary_key）不可读，我没法验签，"
                "也就不能确定日记内容是不是真的——请管理员检查插件数据目录里的密钥文件。"
            )
            return
        # 先做全量 zip 健康检查：中央目录还在、单篇 CRC 坏掉时，
        # is_zipfile() 仍会返回 True，必须先 testzip() 拦下来。
        corrupt = self._corrupt_notice(sender_id)
        if corrupt:
            yield event.plain_result(corrupt)
            return
        latest_content, latest_tampered, latest_name = self._load_latest(sender_id)
        if latest_tampered and latest_name is None:
            # 验签失败属于安全结论，保持固定文案（与同一指令其它验签提示一致）
            yield event.plain_result(
                "📖 日记在插件之外被修改过，已被判定失效、不再使用。"
                "发送 /日记 让我重新写一篇吧。"
            )
            return
        entries = self._list_entries(sender_id)
        if not entries:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 还没有写过日记。发送 /日记 让我现在写一篇吧。",
                    "用户想看日记，但还一篇都没写过。用你自己的口吻告诉他，"
                    "并邀请他发 /日记 让你写第一篇。",
                )
            )
            return

        lines = []
        for i, name in enumerate(entries, 1):
            tag = "（最新）" if name == entries[-1] else ""
            lines.append(f"{i}. {self._entry_label(name)}{tag}")
        yield event.plain_result(
            f"📖 你一共有 {len(entries)} 天的日记：\n"
            + "\n".join(lines)
            + "\n\n回复「最新」读最新一天的；回复序号读对应那天的；"
            "回复日期（如 2026-10-23）读那天的日记。60 秒内有效，回复「取消」退出。"
        )

        if not _SESSION_WAITER_OK:
            # 环境不支持会话等待：直接发最新一篇，并提示可用序号/日期参数
            if latest_tampered:
                yield event.plain_result(
                    "📖 最新一篇验签失败，疑似被篡改，已拒绝读取。"
                )
            else:
                # v0.6：图文穿插发送（零图片时与旧版输出逐字节一致）；
                # 页边批注作为第五部分统一放在文末，绝不穿插进正文四部分。
                section = self._qq_annotation_section(sender_id, entries[-1])
                text = f"📖 {self._entry_label(entries[-1])} 的日记：\n\n{latest_content}"
                if section:
                    text += "\n\n" + section
                await self._send_diary_content(event, sender_id, text)
            return

        @session_waiter(timeout=60, record_history_chains=False)
        async def diary_view_waiter(
            controller: SessionController, ev: AstrMessageEvent
        ):
            # P3-14：不可见字符只影响「取消/最新/序号/日期」判断，剥离后不回写消息
            text = self._strip_invisible(ev.message_str or "").strip()
            low = text.lower()
            if self._is_cancel(text):
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 好，不看啦～想再看随时发 /查看日记。",
                            "用户取消了查看日记。用你自己的口吻轻松回应一下，"
                            "告诉他想看随时再来。",
                        )
                    )
                )
                controller.stop()
                return
            targets = None
            if low in ("最新", "latest", "new", "newest"):
                targets = [entries[-1]]
            elif text.isdigit():
                # P3-2：isdigit() 会放行 ² 等上标数字（int() 转不了），
                # 解析失败统一走"没看懂"分支，不能让 ValueError 打断交互。
                try:
                    idx = int(text)
                except (TypeError, ValueError):
                    idx = None
                if idx is not None and 1 <= idx <= len(entries):
                    targets = [entries[idx - 1]]
            else:
                m = re.match(
                    r"^(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?$", text
                )
                if m:
                    y, mo, dd = (int(g) for g in m.groups())
                    name = self._md_entry(f"{y:04d}{mo:02d}{dd:02d}")
                    targets = [name] if name in entries else []

            if targets is None:
                # P3-15：用法提示分支记 repr，便于看到不可见字符
                logger.warning(
                    f"[memory_diary] 查看日记没看懂用户回复，原始消息："
                    f"{(ev.message_str or '')!r}"
                )
                await ev.send(
                    ev.plain_result(
                        "📖 没看懂～回复「最新」、序号，或日期（如 2026-10-23）。"
                    )
                )
                controller.keep(timeout=60, reset_timeout=True)
                return
            if not targets:
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 那一天没有写日记哦。",
                            "用户指定的那一天没有写过日记，用你自己的口吻如实告诉他。",
                        )
                    )
                )
                controller.stop()
                return
            for name in targets:
                content, tampered = self._load_entry(sender_id, name)
                if tampered:
                    await ev.send(
                        ev.plain_result(
                            f"📖 {self._entry_label(name)} 这篇日记验签失败，疑似被篡改，已拒绝读取。"
                        )
                    )
                else:
                    # v0.6：图文穿插发送（零图片时与旧版输出逐字节一致）；
                    # 页边批注作为第五部分统一放在文末，绝不穿插进正文四部分。
                    section = self._qq_annotation_section(sender_id, name)
                    text = f"📖 {self._entry_label(name)} 的日记：\n\n{content}"
                    if section:
                        text += "\n\n" + section
                    await self._send_diary_content(ev, sender_id, text)
            controller.stop()

        try:
            await diary_view_waiter(event)
        except TimeoutError:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 查看超时啦，想再看随时发 /查看日记。",
                    "查看日记的互动超时了。用你自己的口吻告诉他，想看的话随时再发 /查看日记。",
                )
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 查看日记交互异常: {e}")
        finally:
            event.stop_event()
