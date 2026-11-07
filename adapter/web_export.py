# -*- coding: utf-8 -*-
"""日记本 Web 页面后端：导出 / 一键备份 / 保存到本机下载目录。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

import asyncio
import os
import secrets
import shutil
import subprocess
import zipfile
from pathlib import Path

from ..memory_diary.errors import DiaryKeyUnavailable
from ..memory_diary.log import logger
from .compat import _web_error, _web_file, _web_json, _web_request


class WebExportApiMixin:
    """日记本 Web 页面后端：导出 / 一键备份 / 保存到本机下载目录。"""

    def _web_prepare_export(self, file_key: str):
        """web_export / web_export_local 共用的前半段：
        校验标识、解析主人、过密码闸、打包。成功返回 (临时包路径, None)，
        失败返回 (None, 错误响应)。"""
        if not self._valid_file_key(file_key):
            return None, _web_error("无效的用户标识", status_code=400)
        path = self.data_dir / f"diary_{file_key}.zip"
        if not path.exists():
            return None, _web_error("日记文件不存在", status_code=404)
        qq_hint = (_web_request.query.get("qq", "") or "").strip()
        owner, err = self._resolve_zip_owner(path, file_key, qq_hint)
        if not owner:
            if err == "OWNER_UNKNOWN":
                return None, _web_error("OWNER_UNKNOWN", status_code=400)
            return None, _web_error(f"无法确认日记主人：{err}", status_code=400)
        gate = self._check_web_password(owner, file_key, for_download=True)
        if gate is not None:
            return None, gate
        try:
            with self._diary_zip(path) as z:
                entries = [n for n in z.namelist() if self.ENTRY_NAME_RE.match(n)]
            if not entries:
                return None, _web_error("这个用户还没有日记", status_code=400)
            bundle = self._build_export_bundle(owner, path, entries)
        except DiaryKeyUnavailable:
            return None, _web_error("签名密钥不可用，无法导出", status_code=500)
        except Exception as e:
            logger.error(f"[memory_diary] Web 导出失败: {e}")
            return None, _web_error("导出打包失败，详见后台日志", status_code=500)
        return bundle, None

    async def web_export(self, file_key: str):
        bundle, err_resp = self._web_prepare_export(file_key)
        if err_resp is not None:
            return err_resp
        # file_response 在 handler 返回后才读文件，立即删除会竞态——
        # 按包大小延迟清理（与 web_export_all 同一套 _bundle_cleanup_delay：
        # 固定 120 秒对多年日记的大包/慢网下载可能不够，包被提前删掉会让下载
        # 损坏且无法重试）；迁移包含签名钥匙，不能长期留在磁盘上
        try:
            asyncio.get_running_loop().call_later(
                self._bundle_cleanup_delay(bundle),
                lambda: bundle.unlink(missing_ok=True),
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 迁移包延迟清理注册失败: {e}")
        return _web_file(bundle, filename=bundle.name,
                         content_type="application/zip")

    @staticmethod
    def _bundle_cleanup_delay(bundle: Path) -> int:
        """临时包的延迟清理秒数：基础 600 秒 + 每 MB 1 秒，封顶 1 小时。

        ``file_response`` 在 handler 返回后才读文件，下载期间包必须存在。
        固定 120 秒对大包（多年日记、全量备份、慢网/移动端）可能不够——包被
        提前删掉会让下载损坏且无法重试；下载被截断的代价高于多留一会儿。
        注意两类包：一键备份包不含签名钥匙；单用户导出包含派生钥匙
        （user_key.txt），本函数对它按同一规则延长保留，是为了保证下载完整，
        而不是认为它“不含钥匙”；单用户导出若要更短保留时间，需先确认下载已完成。
        """
        try:
            size_mb = bundle.stat().st_size // (1024 * 1024)
        except OSError:
            size_mb = 0
        return min(3600, 600 + size_mb)

    def _prepare_backup_all(self):
        """打包全部用户日记（不含签名钥匙）；返回 (bundle, 用户数, 错误响应)。"""
        paths = []
        for path in sorted(self.data_dir.glob("diary_*.zip")):
            file_key = path.stem[len("diary_"):]
            if self._valid_file_key(file_key):
                paths.append(path)
        if not paths:
            return None, 0, _web_error("还没有任何用户的日记可备份", status_code=400)
        stamp = self._now().strftime("%Y%m%d_%H%M%S")
        bundle = (
            self.data_dir
            / f"memory_diary_backup_{stamp}_{secrets.token_hex(2)}.zip"
        )
        try:
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
                for p in paths:
                    z.write(p, p.name)
                z.writestr("备份说明.txt", self._backup_readme_text(len(paths)))
        except Exception as e:
            bundle.unlink(missing_ok=True)
            logger.error(f"[memory_diary] 一键备份打包失败: {e}")
            return None, 0, _web_error("备份打包失败，详见后台日志", status_code=500)
        return bundle, len(paths), None

    async def web_export_all(self):
        """一键备份全部用户日记：数据目录下所有 diary_*.zip 打成单个 zip 下载。

        只含日记 zip，绝不含签名钥匙（"绝不入包"原则不变）；包内附 备份说明.txt。
        继承面板登录鉴权，等价管理员本机操作，不加额外密码闸。
        P1-5 产品口径（用户拍板）：查看密码只保护单个用户的翻阅/单用户导出；
        「一键备份全部」属管理员信任域例外，**不**套查看密码闸——能登管理面板
        的人本来就能读到这些 zip。文档需同步说明这个例外。
        """
        bundle, count, err = self._prepare_backup_all()
        if err is not None:
            return err
        # file_response 在 handler 返回后才读文件：按包大小延迟清理（见
        # _bundle_cleanup_delay 的注释；固定 120 秒对大包可能不够）
        try:
            asyncio.get_running_loop().call_later(
                self._bundle_cleanup_delay(bundle),
                lambda: bundle.unlink(missing_ok=True),
            )
        except Exception as e:
            logger.warning(f"[memory_diary] 备份包延迟清理注册失败: {e}")
        logger.info(
            f"[memory_diary] 已打出一键备份包：{count} 个用户的日记"
        )
        return _web_file(bundle, filename=bundle.name,
                         content_type="application/zip")

    # 回环地址集合（AstrBot 4.x 的插件 Web 请求暴露的是 ``client_host``，
    # 不是 ``remote_addr``；见 astrbot.api.web.PluginRequest）。
    _WEB_LOOPBACKS = ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")

    def _web_is_local_panel(self) -> bool:
        """面板请求是否真的来自服务器本机。

        「存到本机并打开位置」会写管理员主目录并启动文件管理器，必须只在
        本机操作时可用。这里用 ``client_host`` 判断回环地址，并对反向代理
        转发头做二次校验：**只信最后一跳**——``X-Forwarded-For`` 链由离本机
        最近的代理追加，首项是客户端可以随便伪造的，拿首项判本机等于没判
        （远程面板伪造 ``X-Forwarded-For: 127.0.0.1`` 就能触发服务器落盘）。
        """
        try:
            host = str(getattr(_web_request, "client_host", "") or "").strip().lower()
        except Exception:
            return False
        if host not in self._WEB_LOOPBACKS:
            return False
        try:
            headers = getattr(_web_request, "headers", None)
        except Exception:
            headers = None
        forwarded = ""
        if headers is not None:
            try:
                xff = str(headers.get("x-forwarded-for") or "").strip()
                real_ip = str(headers.get("x-real-ip") or "").strip()
            except Exception:
                xff = real_ip = ""
            if xff:
                # 只看最后一跳：那是最近一层代理看到的直连客户端地址
                hops = [h.strip().lower() for h in xff.split(",") if h.strip()]
                forwarded = hops[-1] if hops else ""
            elif real_ip:
                forwarded = real_ip.lower()
        return not forwarded or forwarded in self._WEB_LOOPBACKS

    @staticmethod
    def _open_file_location(target: Path) -> None:
        """在本机文件管理器中打开并选中文件（失败只记日志，不影响已保存文件）。"""
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(target)])
            elif os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
                subprocess.Popen(["open", "-R", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target.parent)])
        except Exception as e:
            logger.warning(f"[memory_diary] 打开文件位置失败（文件已保存）: {e}")

    def _save_bundle_to_downloads(self, bundle: Path) -> Path:
        """把临时包移到本机「下载」文件夹（重名自动加序号），并打开文件位置。"""
        downloads = Path.home() / "Downloads"
        if not downloads.is_dir():
            downloads = Path.home()
        target = downloads / bundle.name
        n = 1
        while target.exists():
            target = downloads / f"{bundle.stem}_{n}{bundle.suffix}"
            n += 1
        shutil.move(str(bundle), str(target))
        self._open_file_location(target)
        return target

    async def web_export_all_local(self):
        """一键备份全部用户日记 → 存到本机「下载」文件夹并打开位置。

        与「备份全部日记（浏览器下载）」内容完全一致（只含日记 zip、附备份说明、
        绝不含签名钥匙）；区别是后端把包直接落到服务器本机下载目录并唤起文件管理器，
        因此和单用户 `web_export_local` 一样只允许在本机面板访问时使用。
        """
        if not self._web_is_local_panel():
            return _web_error(
                "REMOTE_PANEL：管理面板不在服务器本机，请改用「下载到浏览器」",
                status_code=400,
            )
        bundle, count, err = self._prepare_backup_all()
        if err is not None:
            return err
        try:
            target = self._save_bundle_to_downloads(bundle)
        except Exception as e:
            bundle.unlink(missing_ok=True)
            logger.error(f"[memory_diary] 一键备份保存到下载目录失败: {e}")
            return _web_error("保存到下载文件夹失败，详见后台日志", status_code=500)
        logger.info(
            f"[memory_diary] 一键备份已存到本机：{count} 个用户的日记 → {target.name}"
        )
        return _web_json({"saved_to": str(target), "filename": target.name})

    async def web_export_local(self, file_key: str):
        """存到本机「下载」文件夹并打开文件位置。

        浏览器没有权限打开用户的文件管理器，但面板就开在服务器本机
        （localhost、且没有反向代理转发）时，后端可以代劳。远程访问面板时
        拒绝并提示改用「下载到浏览器」。
        """
        if not self._web_is_local_panel():
            return _web_error(
                "REMOTE_PANEL：管理面板不在服务器本机，请改用「下载到浏览器」",
                status_code=400,
            )
        bundle, err_resp = self._web_prepare_export(file_key)
        if err_resp is not None:
            return err_resp
        try:
            target = self._save_bundle_to_downloads(bundle)
        except Exception as e:
            bundle.unlink(missing_ok=True)
            logger.error(f"[memory_diary] 迁移包保存到下载目录失败: {e}")
            return _web_error("保存到下载文件夹失败，详见后台日志", status_code=500)
        return _web_json({"saved_to": str(target), "filename": target.name})
