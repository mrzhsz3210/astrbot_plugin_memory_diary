# -*- coding: utf-8 -*-
"""启动清理：上次异常退出残留的临时导出/备份包与插图临时文件。

由 ``main.MemoryDiaryPlugin`` 以 mixin 方式组合；带 ``@filter.*`` 装饰器的入口
必须留在 main.py（AstrBot 按 ``handler.__module__`` 绑定 handler），这里只放实现。
"""

from ..memory_diary.log import logger


class LifecycleMixin:
    """启动清理：上次异常退出残留的临时导出/备份包与插图临时文件。"""

    # ------------------------- 启动清理（P3-7） -------------------------

    # 导出/一键备份的临时包前缀：正常路径都在下载后延迟删除，能留到下次启动的
    # 只可能是进程崩溃/被杀（export 包含用户派生签名钥匙，backup 是全部用户日记）。
    _STALE_BUNDLE_GLOBS = (
        "memory_diary_export_*.zip",
        "memory_diary_backup_*.zip",
    )

    async def initialize(self):
        """插件启动/激活：先清掉上次异常退出残留的临时包/插图临时文件，再走原有初始化。"""
        try:
            self._purge_stale_bundles()
        except Exception as e:
            logger.warning(f"[memory_diary] 启动清理残留临时包失败: {e}")
        try:
            self._purge_temp_images()
        except Exception as e:
            logger.warning(f"[memory_diary] 启动清理插图临时文件失败: {e}")
        try:
            self._companion_evidence_expire()
        except Exception as e:
            logger.warning(f"[memory_diary] 启动清理过期陪伴存证失败: {e}")
        try:
            self._purge_trash()
        except Exception as e:
            logger.warning(f"[memory_diary] 启动清理过期回收站失败: {e}")
        try:
            # v0.7.6（Bug C）：外部删除/篡改扫描——登记包凭空消失或
            # 全量验签失败时打 ERROR 日志（/日记状态 与 WebUI 同步明示）
            self._external_damage_scan()
        except Exception as e:
            logger.warning(f"[memory_diary] 启动外部破坏扫描失败: {e}")
        await super().initialize()

    def _purge_temp_images(self) -> None:
        """删除 data_dir/temp 下残留的插图临时文件（v0.6）。

        插图临时文件只在"从 zip 读图 → 交给 Image 组件发送"的瞬间存在，正常路径
        finally 删除；能留到下次启动的只可能是进程崩溃/强杀。启动时没有在途任务，
        清空整个 temp 目录是安全的（该目录只由插图发送与推送使用）。
        """
        temp_dir = self.data_dir / "temp"
        if not temp_dir.is_dir():
            return
        removed = 0
        for path in temp_dir.iterdir():
            if not path.is_file():
                continue
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except Exception as e:
                logger.warning(f"[memory_diary] 删除残留插图临时文件失败 {path}: {e}")
        if removed:
            logger.info(
                f"[memory_diary] 启动清理：已删除 {removed} 个残留的插图临时文件"
            )

    def _purge_stale_bundles(self) -> None:
        """删除数据目录里残留的临时导出/备份包（P3-7）。

        这两类文件只在“打包 → 交给 Web 下载 → 延迟删除”期间存在；插件重新
        启动时数据目录里还能看到的，都是上一次进程崩溃/强杀留下的残骸。
        export 包带用户签名钥匙、backup 包带全部用户日记，都不应长期留在磁盘。

        为什么不怕误删“重载插件时正在下载的在途包”：下载期间 Starlette 的
        FileResponse 持有已打开的文件句柄——Windows 上 unlink 会因共享冲突抛
        PermissionError（下面的 except 会跳过它），POSIX 上 unlink 后已打开的 fd
        仍读原 inode，下载不会中断。只有“包刚建好、还没开始读文件”的毫秒级窗口
        才可能踩到。改成按 pid/mtime 判活虽然也能清残骸，但需要额外区分“重载的在途
        窗口”和“崩溃残骸”（两者对 pid/mtime 的判定并不一致），收益低、代码风险高，
        故保持“启动即清”。
        """
        removed = 0
        for pattern in self._STALE_BUNDLE_GLOBS:
            for path in self.data_dir.glob(pattern):
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except Exception as e:
                    logger.warning(f"[memory_diary] 删除残留临时包失败 {path}: {e}")
        if removed:
            logger.info(
                f"[memory_diary] 启动清理：已删除 {removed} 个残留的临时导出/备份包"
            )
