# -*- coding: utf-8 -*-
"""指令实现：/导出日记、/导入日记（迁移包）。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

from pathlib import Path

from astrbot.api.event import AstrMessageEvent

from ..memory_diary.log import logger
from .compat import SessionController, session_waiter, _SESSION_WAITER_OK


class TransferCommandsMixin:
    """指令实现：/导出日记、/导入日记（迁移包）。"""

    # ------------------------- 指令：导出日记（迁移包） -------------------------
    #
    # 目的：云端服务器的用户不用 SSH，私聊发 /导出日记 即可拿到迁移包。
    # 签名体系：主密钥 .diary_key 只在服务器上，每个用户的签名钥匙由它按
    # QQ 号派生（user-key:<QQ>）。迁移包只带当事人这把派生钥匙——
    # 泄露也仅能动这一份日记，因此普通用户也可以安全地自助导出。
    # 安全约束：
    #   · 仅私聊——钥匙按 QQ 绑定，群聊不开放；
    #   · 临时包发完即删，不在数据目录留第二份钥匙副本；
    #   · 日常存储的日记 zip 仍然不含任何钥匙（"绝不入包"的设计不变）。

    async def _send_export_file(self, event: AstrMessageEvent, path: Path) -> bool:
        """把文件发到当前私聊。优先 NapCat 原生上传 API，回退标准 File 组件。"""
        # 路径 1：aiocqhttp / NapCat 原生接口（新版 NapCat 不支持消息内嵌 file 段，
        # 会报 file segment without file_id is unsupported）
        client = getattr(event, "bot", None)
        if client is not None:
            try:
                await client.api.call_action(
                    "upload_private_file",
                    user_id=int(event.get_sender_id()),
                    file=str(path.resolve()),
                    name=path.name,
                )
                return True
            except Exception as e:
                logger.warning(
                    f"[memory_diary] upload_private_file 失败，回退 File 组件: {e}"
                )
        # 路径 2：AstrBot 标准 File 组件（其他平台兜底）
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import File

            await event.send(
                MessageChain(chain=[File(file=str(path), name=path.name)])
            )
            return True
        except Exception as e:
            logger.error(f"[memory_diary] File 组件发送也失败: {e}")
            return False

    async def _handle_export_diary(self, event: AstrMessageEvent):
        """导出自己的迁移包（仅私聊）：日记 zip + 本人的派生签名钥匙，发 QQ 文件。（``main.export_diary`` 的实现）"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 导出日记只能私聊操作哦。",
                    "用户在群里想导出日记，你要委婉地拒绝：导出包含私密内容，请他来私聊操作。",
                )
            )
            return

        sender_id = event.get_sender_id()
        if self._secret_key() is None:
            yield event.plain_result(
                "📖 签名密钥（.diary_key）不可读，没法导出——"
                "请管理员检查插件数据目录里的密钥文件。"
            )
            return
        corrupt = self._corrupt_notice(sender_id)
        if corrupt:
            yield event.plain_result(corrupt)
            return
        diary_path = self._diary_path(sender_id)
        entries = self._list_entries(sender_id)
        if not diary_path.exists() or not entries:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 还没有写过日记，没什么可导出的。发送 /日记 让我先写一篇吧。",
                    "用户想导出日记，但还一篇都没写过、没什么可导的。"
                    "用你自己的口吻告诉他，并邀请他先发 /日记 让你写一篇。",
                )
            )
            return

        export_path = None
        try:
            export_path = self._build_export_bundle(sender_id, diary_path, entries)

            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    f"📖 打包好啦：{len(entries)} 天的日记 + 只属于你的签名钥匙，现在发给你。\n"
                    "这把钥匙动得了你这份日记（所以别转发给别人），但碰不了别人的日记。",
                    f"用户要导出日记，迁移包已经打好了：{len(entries)} 天的日记 + 只属于他的签名钥匙。"
                    f"请用你自己的口吻告诉他马上把包发过去（必须保留 {len(entries)} 天这个数字），"
                    "并提醒他：这把钥匙只动得了自己这份日记，但也别转发给别人。两三句话以内。",
                )
            )
            if await self._send_export_file(event, export_path):
                yield event.plain_result(
                    await self._say(
                        event.unified_msg_origin,
                        "📖 迁移包已发出～到新设备后按包里的「导入说明.txt」操作，"
                        "签名会原样通过，不用重新签。",
                        "迁移包已经通过私聊文件发给用户了。用你自己的口吻告诉他："
                        "到新设备后按包里的「导入说明.txt」操作就行，签名会原样通过。",
                    )
                )
            else:
                yield event.plain_result(
                    "📖 包打好了但发送失败（协议端可能读不到这个文件路径，"
                    "比如 AstrBot 和 NapCat 分属两个没有共享卷的容器）。"
                    "详情请见后台日志。"
                )
        except Exception as e:
            logger.error(f"[memory_diary] 导出日记失败: {e}")
            yield event.plain_result("📖 导出失败了，具体原因请管理员看后台日志。")
        finally:
            # 导出包含签名钥匙，绝不在数据目录留第二份副本
            if export_path is not None:
                try:
                    export_path.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(
                        f"[memory_diary] 导出临时包删除失败，请手动清理 {export_path}: {e}"
                    )
        event.stop_event()

    # ------------------------- 指令：导入日记（迁移包） -------------------------
    #
    # 与 /导出日记 配套的接收端：用户把迁移包当 QQ 文件发回来即可，全程不碰文件夹。
    # 安全顺序：先解开包做全量校验（manifest 格式 + zip CRC + 包内密钥逐篇验签），
    # 全部通过才允许接触本机的密钥与日记文件——不信任的包一个字节都不落盘。

    async def _handle_import_diary(self, event: AstrMessageEvent):
        """导入自己的迁移包（仅私聊）：直接把 /导出日记 收到的迁移包发给我。（``main.import_diary`` 的实现）"""
        if not self._should_respond(event):
            return
        if event.get_group_id():
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 导入日记只能私聊操作哦。",
                    "用户在群里想导入日记迁移包。请用你自己的口吻委婉拒绝："
                    "导入涉及签名钥匙和私密日记，请他来私聊操作。",
                )
            )
            return
        if not _SESSION_WAITER_OK:
            yield event.plain_result(
                "📖 当前环境不支持交互操作——请管理员在管理面板的「日记本」"
                "页面用「导入日记」上传迁移包。"
            )
            return

        yield event.plain_result(
            await self._say(
                event.unified_msg_origin,
                "📖 请把迁移包（memory_diary_export_*.zip）作为文件发给我，"
                "120 秒内有效，回复「取消」退出。\n"
                "我会先验签确认它没被改过，再安放日记。",
                "用户要导入日记迁移包。请用你自己的口吻请他把迁移包 zip 作为"
                "「文件」（不是文字或图片）发给你，告诉他 120 秒内有效、回复「取消」退出；"
                "你会先验签确认没被改过再安放日记。"
                "必须保留「文件」「120 秒」「取消」这几个信息，两三句话以内。",
            )
        )

        @session_waiter(timeout=120, record_history_chains=False)
        async def diary_import_waiter(
            controller: SessionController, ev: AstrMessageEvent
        ):
            # P3-14：取消口令要先剥不可见字符再判断（只判断，不回写消息）
            if self._is_cancel(self._strip_invisible(ev.message_str or "")):
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 已取消导入，什么也没改动。",
                            "用户取消了导入迁移包的操作，什么都没改动。"
                            "用你自己的口吻确认一下就好。",
                        )
                    )
                )
                controller.stop()
                return
            try:
                from astrbot.api.message_components import File as _FileComp
            except ImportError:  # pragma: no cover
                _FileComp = None
            file_comp = None
            if _FileComp is not None:
                for comp in ev.get_messages():
                    if isinstance(comp, _FileComp):
                        file_comp = comp
                        break
            if file_comp is None:
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 没收到文件哦——请把迁移包 zip 作为「文件」发给我。",
                            "用户说要导入日记，但没有发文件过来。请用你自己的口吻提醒他："
                            "要把迁移包 zip 作为「文件」（不是文字或图片）发给你。",
                        )
                    )
                )
                controller.keep(timeout=120, reset_timeout=True)
                return

            downloaded = ""
            try:
                downloaded = await file_comp.get_file()
            except Exception as e:
                logger.warning(f"[memory_diary] 迁移包下载失败: {e}")
            if not downloaded:
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            "📖 文件下载失败了，请重发一次迁移包。",
                            "迁移包下载失败了。请用你自己的口吻让他重发一次迁移包，"
                            "语气温柔一点。",
                        )
                    )
                )
                controller.keep(timeout=120, reset_timeout=True)
                return

            # P2-1：QQ 侧同样先看压缩包本身体积，超大直接拒（解压在 _verify_bundle
            # 里还有解压后总量/压缩比双闸）
            try:
                if Path(downloaded).stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("迁移包文件超过 32MB 上限，已拒绝导入")
            except FileNotFoundError:
                pass
            try:
                owner_id, key, entries, images, annotations, provenance, current_self, materials, anchor_archive = (
                    self._verify_bundle(Path(downloaded))
                )
                sender_name = ""
                try:
                    sender_name = ev.get_sender_name() or ""
                except Exception:
                    pass
                mode = self._install_bundle(
                    ev.get_sender_id(), owner_id, key, entries, sender_name,
                    images=images, annotations=annotations,
                    provenance=provenance, current_self=current_self, materials=materials,
                    anchor_archive=anchor_archive,
                )
                n_days = len(entries)
                if mode == "same_key":
                    msg = (
                        f"📖 导入完成！{n_days} 天的日记已安放"
                        "（签名钥匙与本机一致，原样有效）。\n"
                        "发 /查看日记 就能看到她写的日记啦。"
                    )
                    key_fact = "签名钥匙与本机一致，原签名原样有效"
                else:
                    msg = (
                        f"📖 导入完成！{n_days} 天的日记已用本机的签名钥匙"
                        "重新签名后安放，本机原有的日记不受影响。\n"
                        "发 /查看日记 就能看到她写的日记啦。"
                    )
                    key_fact = (
                        "已用本机的签名钥匙重新签名后安放，"
                        "本机原有的日记不受影响"
                    )
                logger.info(
                    f"[memory_diary] 用户 {ev.get_sender_id()} 导入迁移包成功："
                    f"{n_days} 天日记，模式 {mode}"
                )
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            msg,
                            f"用户的迁移包导入成功了：{n_days} 天的日记已经安放"
                            f"（{key_fact}）。请用你自己的口吻告诉他导入完成"
                            f"（必须保留 {n_days} 天这个数字与签名相关的说明），"
                            "并邀请他发 /查看日记 看日记。两三句话以内。",
                        )
                    )
                )
            except ValueError as e:
                await ev.send(
                    ev.plain_result(
                        await self._say(
                            ev.unified_msg_origin,
                            f"📖 导入被拒绝：{e}",
                            f"用户导入的迁移包被拒绝了，原因是：{e}。"
                            "请用你自己的口吻把拒绝原因如实告诉他"
                            "（必须原样保留原因里的关键信息，不要自己补充判断），"
                            "语气认真一点，两三句话以内。",
                        )
                    )
                )
            except Exception as e:
                logger.error(f"[memory_diary] 导入日记失败: {e}")
                await ev.send(
                    ev.plain_result("📖 导入失败了，具体原因请管理员看后台日志。")
                )
            finally:
                # 下载的迁移包含签名钥匙，用完即删
                try:
                    Path(downloaded).unlink(missing_ok=True)
                except Exception:
                    pass
            controller.stop()

        try:
            await diary_import_waiter(event)
        except TimeoutError:
            yield event.plain_result(
                await self._say(
                    event.unified_msg_origin,
                    "📖 导入超时啦，想导入随时再发 /导入日记。",
                    "导入日记的互动超时了，什么也没改动。用你自己的口吻告诉他，"
                    "想导入的话随时再发 /导入日记。",
                )
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 导入日记交互异常: {e}")
        finally:
            event.stop_event()
