# -*- coding: utf-8 -*-
"""v0.5.4 → v0.5.5 升级兼容性回归（真实跨版本数据目录 + 旧配置）。

差分套件每次都在两个隔离数据目录里分别跑 v0.5.4 与 v0.5.5，但它并不验证
“v0.5.4 写下的数据目录，v0.5.5 能不能直接读/续写/验签”。这里专门验证：

1. v0.5.4 写出的日记 zip（文件名、签名、说明）能被 v0.5.5 直接读取并通过验签，
   且 v0.5.5 续写后 v0.5.4 仍能读回、验签通过——数据格式与密钥派生两向连续；
2. v0.5.4 的旧配置（缺 v0.5.5 新增键、含已删除的 bot_call_names）交给 v0.5.5
   不报错，缺省行为安全（实验性估算默认关闭、白名单留空放行）。
"""

from __future__ import annotations

import os

from harness import env, loader
from harness.errors import SkipTest
from harness.fakes import make_event
from harness.world import DIARY_TEXT, World, message

SENDER = "10001"
UMO = "aiocqhttp:FriendMessage:10001"


def _require_both():
    if env.is_baseline_only():
        raise SkipTest("needs both baseline(v0.5.4) and refactor(v0.5.5)")


def _open_v055_on(data_root: str, world: World):
    """在指定 ASTRBOT_ROOT 上实例化 v0.5.5 插件（用于复用 v0.5.4 的数据目录）。"""
    refactor = world.refactor
    previous = os.environ.get("ASTRBOT_ROOT")
    os.environ["ASTRBOT_ROOT"] = data_root
    try:
        plugin = refactor.module.MemoryDiaryPlugin(
            world.baseline.ctx, world.baseline.config
        )
        # 与 Instance 一致：用固定时钟，保证 v0.5.5 续写落在同一天
        plugin._now = lambda: world.now
        return plugin
    finally:
        if previous is None:
            os.environ.pop("ASTRBOT_ROOT", None)
        else:
            os.environ["ASTRBOT_ROOT"] = previous


async def test_v054_data_dir_readable_and_writable_by_v055():
    _require_both()
    async with World() as world:
        baseline = world.baseline
        # 1) v0.5.4 写出日记
        baseline.plugin._save_diary(
            SENDER, DIARY_TEXT, sender_name="阿罗", origin="private", mode="full"
        )
        base_dir = str(baseline.data_dir)

        # 2) v0.5.5 直接打开 v0.5.4 的数据目录：文件名、读取、验签都一致
        plugin = _open_v055_on(str(baseline.root), world)
        assert str(plugin.data_dir) == base_dir
        assert plugin._diary_path(SENDER) == baseline.plugin._diary_path(SENDER)
        assert plugin._list_entries(SENDER) == ["diaries/20261023.md"]
        content, tampered, name = plugin._load_latest(SENDER)
        assert name == "diaries/20261023.md"
        assert tampered is False, "v0.5.5 必须能验通 v0.5.4 写下的签名"
        # 日记版本会加片段分隔条，正文内容必须原样可读
        assert DIARY_TEXT.strip() in content

        # 3) v0.5.5 在旧数据目录里续写，v0.5.4 仍能读回并验签
        plugin._save_diary(
            SENDER, "续写的一条简记。", sender_name="阿罗", origin="private",
            mode="brief",
        )
        content2, tampered2, _ = baseline.plugin._load_latest(SENDER)
        assert tampered2 is False, "v0.5.4 必须能验通 v0.5.5 续写后的签名"
        assert "续写的一条简记。" in content2
        assert DIARY_TEXT.split("\n")[0] in content2


async def test_v054_config_without_new_keys_still_works():
    _require_both()
    async with World(config={"use_persona": False, "diary_op_interval": 0}) as world:
        inst = world.refactor
        plugin = inst.plugin
        # 模拟 v0.5.4 配置：没有 v0.5.5 新增键，且仍带已删除的 bot_call_names
        for key in ("token_estimation_v2", "token_calibration_enabled", "allowed_users"):
            plugin.config.pop(key, None)
        plugin.config["bot_call_names"] = "小深"

        # 旧配置缺省：两项实验性能力关闭、白名单为空=放行、未知键不炸
        assert plugin._token_estimation_v2() is False
        assert plugin._calibration_enabled() is False
        assert plugin._is_allowed("99999") is True
        assert plugin._watch_ratios() == (0.75, 0.9)

        # 旧配置仍能正常写日记
        inst.set_history(
            [message("user", "今天聊了考试"), message("assistant", "加油")]
        )
        inst.provider.replies[:] = [DIARY_TEXT]
        out = "\n".join(
            await inst.run_handler(
                "write_diary_cmd", make_event("/日记 完整", sender_id=SENDER, umo=UMO)
            )
        )
        assert "日记写好啦" in out
        assert inst.entries(SENDER) == ["diaries/20261023.md"]

        # 旧配置里 enable_context_watch=false 的迁移路径仍生效（视为两档 0）
        plugin.config["enable_context_watch"] = False
        assert plugin._watch_ratios() == (0.0, 0.0)
