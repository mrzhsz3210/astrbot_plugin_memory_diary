# -*- coding: utf-8 -*-
"""日记本 Web 页面后端：界面偏好（字号/字体）与自定义纸张/背景板。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

import base64
import json
import os
import re
import secrets
from pathlib import Path

from ..memory_diary.log import logger
from .compat import _PluginUploadFile, _web_error, _web_json, _web_request


class WebPrefsApiMixin:
    """日记本 Web 页面后端：界面偏好（字号/字体）与自定义纸张/背景板。"""

    # 界面偏好（字号/字体）：iframe 沙箱没有 localStorage，
    # 存到数据目录、按管理面板账号分开；视图加载时读取，改动即保存。
    _VIEW_FONT_FAMILIES = ("kai", "song", "sans")

    def _view_prefs_path(self) -> Path:
        return self.data_dir / "web_view_prefs.json"

    def _load_view_prefs(self) -> dict:
        try:
            d = json.loads(self._view_prefs_path().read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    async def web_get_prefs(self):
        username = getattr(_web_request, "username", None) or "_default"
        prefs = self._load_view_prefs().get(username)
        return _web_json({
            "prefs": prefs if isinstance(prefs, dict) else {},
            "brand": self._web_brand(),
        })

    async def web_save_prefs(self):
        payload = await _web_request.json(default={})
        prefs = payload.get("prefs") if isinstance(payload, dict) else None
        if not isinstance(prefs, dict):
            return _web_error("无效的偏好内容", status_code=400)
        clean = {}
        # 白名单 + 范围钳制：不信任视图传来的任何值
        try:
            size = int(prefs.get("font_size", 16))
            clean["font_size"] = min(max(size, 13), 22)
        except (TypeError, ValueError):
            pass
        family = str(prefs.get("font_family", ""))
        if family in self._VIEW_FONT_FAMILIES:
            clean["font_family"] = family
        paper = str(prefs.get("paper", ""))
        if paper in ("default", "custom"):
            clean["paper"] = paper
        backdrop = str(prefs.get("backdrop", ""))
        if backdrop in ("default", "custom"):
            clean["backdrop"] = backdrop
        username = getattr(_web_request, "username", None) or "_default"
        try:
            all_prefs = self._load_view_prefs()
            all_prefs[username] = clean
            p = self._view_prefs_path()
            tmp = p.with_name(f"{p.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
            tmp.write_text(json.dumps(all_prefs, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except Exception as e:
            logger.warning(f"[memory_diary] 保存界面偏好失败: {e}")
            return _web_error("保存失败，详见后台日志", status_code=500)
        return _web_json({"saved": True, "prefs": clean})

    # ---- 自定义纸张 / 背景板：图片存数据目录，读取时以 base64 JSON 返回 ----
    # （iframe 沙箱不能直接带鉴权拉二进制，bridge 只能传 JSON，故用 data URL）
    # kind 取值 "paper"（书页纸纹）或 "backdrop"（书本背后的背景板），互不影响。
    _PAPER_MIMES = {"image/png", "image/jpeg", "image/webp"}
    _PAPER_MAX_BYTES = 3 * 1024 * 1024
    _WEB_IMAGE_KINDS = ("paper", "backdrop")

    def _web_brand(self) -> str:
        """日记本页面左上角称呼（配置项 web_diary_brand），默认 TA，最长 12 字符。"""
        name = str(self.config.get("web_diary_brand", "") or "").strip()
        name = re.sub(r"\s+", "", name)[:12]
        return name or "TA"

    def _web_image_paths(self, kind: str):
        return (
            self.data_dir / f"web_{kind}.img",
            self.data_dir / f"web_{kind}.json",
        )

    async def _web_get_image(self, kind: str):
        img_path, meta_path = self._web_image_paths(kind)
        if not img_path.exists():
            return _web_json({"found": False})
        try:
            mime = "image/png"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("mime") in self._PAPER_MIMES:
                    mime = meta["mime"]
            data = base64.b64encode(img_path.read_bytes()).decode("ascii")
            # 字段名不能叫 data：AstrBot 插件视图的 bridge 会把响应体顶层的 data
            # 当成信封解包（PluginViewPage.vue: response.data?.data ?? response.data），
            # 一旦撞名，前端只拿到 base64 字符串，r.found / r.mime 全丢了，
            # 表现就是「上传成功但页面永远不换图」（同款坑见 tests/test_web.py）。
            return _web_json({"found": True, "mime": mime, "image": data})
        except Exception as e:
            logger.warning(f"[memory_diary] 读取自定义{kind}图片失败: {e}")
            return _web_error("读取图片失败，详见后台日志", status_code=500)

    async def _web_upload_image(self, kind: str):
        files = await _web_request.files()
        upload = files.get("file") if files else None
        if _PluginUploadFile is None or not isinstance(upload, _PluginUploadFile):
            return _web_error("没有收到文件", status_code=400)
        mime = (getattr(upload, "content_type", "") or "").lower().strip()
        if mime not in self._PAPER_MIMES:
            return _web_error("只支持 PNG / JPG / WebP 图片", status_code=400)
        img_path, meta_path = self._web_image_paths(kind)
        tmp = self.data_dir / f"{kind}_tmp_{secrets.token_hex(4)}"
        try:
            await upload.save(tmp)
            if tmp.stat().st_size > self._PAPER_MAX_BYTES:
                return _web_error("图片不能超过 3MB", status_code=400)
            os.replace(tmp, img_path)
            meta_path.write_text(json.dumps({"mime": mime}), encoding="utf-8")
            return _web_json({"saved": True})
        except Exception as e:
            logger.error(f"[memory_diary] 上传自定义{kind}图片失败: {e}")
            return _web_error("保存失败，详见后台日志", status_code=500)
        finally:
            tmp.unlink(missing_ok=True)

    async def web_get_paper(self):
        return await self._web_get_image("paper")

    async def web_upload_paper(self):
        return await self._web_upload_image("paper")

    async def web_get_backdrop(self):
        return await self._web_get_image("backdrop")

    async def web_upload_backdrop(self):
        return await self._web_upload_image("backdrop")
