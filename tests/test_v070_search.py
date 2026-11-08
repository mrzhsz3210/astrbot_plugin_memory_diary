# -*- coding: utf-8 -*-
"""v0.7.0：本地关键词证据搜索、/回忆 与模型工具身份边界。"""

from __future__ import annotations

from harness.fakes import FakeWebRequest, make_event
from harness.world import World
from memory_diary.search import evidence_snippet, search_diaries

SENDER = "10001"


async def test_search_nfkc_casefold_and_filters_and_snippet():
    async with World(config={"local_search_enabled": True}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, "ＡＢＣ 今天讨论 Release", origin="private", mode="full")
        plugin._save_diary(
            SENDER, "明天考试", origin="private", mode="brief", entry_name_hint="20261024"
        )
        result = search_diaries(plugin, SENDER, "ａｂｃ release")
        assert result["total"] == 1
        assert result["results"][0]["day"] == "20261023"
        assert search_diaries(plugin, SENDER, "ABC missing")["total"] == 0
        assert search_diaries(plugin, SENDER, "考试", day="2026-10-24")["total"] == 1
        assert search_diaries(plugin, SENDER, "", mode="brief")["total"] == 1
        snippet = evidence_snippet("开头" + "长" * 300 + "关键词" + "尾" * 300, ["关键词"])
        assert len(snippet) <= 242 and "关键词" in snippet


async def test_search_group_isolation_share_and_tamper_fail_closed():
    async with World(config={"group_share_map": "A=B"}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, "A群发布证据", origin="group", scope="group:A", mode="full")
        plugin._save_diary(SENDER, "B群共享证据", origin="group", scope="group:B", mode="full")
        plugin._save_diary(SENDER, "私聊秘密证据", origin="private", mode="full")
        assert search_diaries(plugin, SENDER, "证据", scope="group:A")["total"] == 2
        assert search_diaries(plugin, SENDER, "证据", scope="group:B")["total"] == 1
        assert all(
            item["scope"] != "private"
            for item in search_diaries(plugin, SENDER, "证据", scope="group:A")["results"]
        )
        path = plugin._diary_path(SENDER)
        import zipfile
        with zipfile.ZipFile(path) as z:
            entries = {name: z.read(name) for name in z.namelist()}
        entries["diaries/20261023.md"] = b"tampered"
        plugin._write_zip_atomic(path, entries)
        result = search_diaries(plugin, SENDER, "证据", scope="group:A")
        assert result["results"] == [] and result["tampered"] is True


async def test_recall_and_llm_tool_inherit_event_identity_and_scope():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, "A群独有旧证据", origin="group", scope="group:A", mode="full")
        plugin._save_diary(SENDER, "私聊旧证据", origin="private", mode="full")
        other = "20002"
        plugin._save_diary(other, "其他用户旧证据", origin="private", mode="full")
        event = make_event("/回忆 旧证据", group_id="A")
        output = await inst.run_handler("recall", event)
        assert "A群独有旧证据" in "\n".join(output)
        assert "私聊旧证据" not in "\n".join(output)
        tool_text = await plugin.memory_diary_search(
            event, "旧证据", max_results=5
        )
        assert "A群独有旧证据" in tool_text
        assert "其他用户旧证据" not in tool_text
        assert "私聊旧证据" not in tool_text
        empty = await inst.run_handler("recall", make_event("/回忆 不存在"))
        assert "不代表没有相关记忆" in empty[0]


async def test_private_recall_includes_own_numbered_and_legacy_group_segments():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(
            SENDER, "编号群的旧群证据", origin="group", scope="group:A", mode="full"
        )
        plugin._save_diary(SENDER, "无编号旧群证据", origin="group", mode="full")
        plugin._save_diary(SENDER, "私聊旧证据", origin="private", mode="full")
        plugin._save_diary("20002", "别人的旧群证据", origin="private", mode="full")
        domain = search_diaries(plugin, SENDER, "证据")
        assert domain["total"] == 3, domain
        event = make_event("/回忆 证据")
        output = "\n".join(await inst.run_handler("recall", event))
        assert "编号群的旧群证据" in output, output
        assert "无编号旧群证据" in output, output
        assert "私聊旧证据" in output, output
        assert "别人的旧群证据" not in output
        tool_text = await plugin.memory_diary_search(event, "证据")
        assert "编号群的旧群证据" in tool_text and "无编号旧群证据" in tool_text
        assert "别人的旧群证据" not in tool_text


async def test_search_switch_disables_all_three_entry_points():
    async with World(config={"local_search_enabled": False, "use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        plugin._save_diary(SENDER, "今天发布", origin="private", mode="full")
        assert search_diaries(plugin, SENDER, "发布")["disabled"] is True
        output = await inst.run_handler("recall", make_event("/回忆 发布"))
        assert "关闭" in output[0]
        assert "不可用" in await plugin.memory_diary_search(make_event(""), "发布")


async def test_search_life_default_excluded_and_self_all_opt_in():
    async with World(config={}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            SENDER,
            "没人来时我独自看云",
            origin="private",
            mode="life",
            separator_note="（无新对话·她的日常随笔）",
        )
        assert search_diaries(plugin, SENDER, "看云")["total"] == 0
        private = search_diaries(plugin, SENDER, "看云", evidence_type="self")
        assert private["total"] == 1
        assert private["results"][0]["evidence_type"] == "self"
        assert private["results"][0]["evidence_label"] == "AI 私人随笔"
        assert search_diaries(plugin, SENDER, "看云", evidence_type="all")["total"] == 1
        assert search_diaries(plugin, SENDER, "看云", evidence_type="invalid")["total"] == 0


async def test_search_mixed_masks_before_matching_and_marks_explicit():
    async with World(config={}) as world:
        plugin = world.refactor.plugin
        self_text = "等回复的间隙，我把我们的对话翻出来又读了一遍。"
        explicit = "请记住我最终报考的是南淮大学。"
        plugin._save_diary(
            SENDER,
            f"她今天确定了报考学校。{explicit}{self_text}",
            origin="private",
            mode="full",
        )
        assert plugin.record_self_provenance(SENDER, "20261023", [self_text])
        assert plugin.record_explicit_provenance(SENDER, "20261023", [explicit])
        result = search_diaries(plugin, SENDER, "报考学校")
        assert result["total"] == 1
        item = result["results"][0]
        assert item["evidence_type"] == "mixed"
        assert item["evidence_label"] == "混合段落"
        assert item["has_explicit"] is True and item["self_masked"] is True
        assert self_text not in item["snippet"]
        assert "〔她的私人随笔〕" in item["snippet"]
        assert search_diaries(plugin, SENDER, "翻出来")["total"] == 0
        private = search_diaries(plugin, SENDER, "翻出来", evidence_type="self")
        assert private["total"] == 1
        assert private["results"][0]["evidence_type"] == "mixed"
        assert "〔她的私人随笔〕" in private["results"][0]["snippet"]
        assert search_diaries(plugin, SENDER, "翻出来", evidence_type="all")["total"] == 1


async def test_search_duplicate_spans_and_same_day_segments_mask_conservatively():
    async with World(config={}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, "第一段用户事实", origin="private", mode="full")
        plugin._save_diary(
            SENDER,
            "第二段用户事实。私人句。其他。私人句。",
            origin="private",
            mode="full",
        )
        assert plugin.record_self_provenance(SENDER, "20261023", ["私人句。"])
        default = search_diaries(plugin, SENDER, "")
        assert default["total"] == 2
        first = next(item for item in default["results"] if "第一段" in item["snippet"])
        second = next(item for item in default["results"] if "第二段" in item["snippet"])
        assert first["evidence_type"] == "user" and not first["self_masked"]
        assert second["evidence_type"] == "mixed"
        assert second["snippet"].count("〔她的私人随笔〕") == 2
        assert "私人句" not in second["snippet"]
        assert search_diaries(plugin, SENDER, "私人句")["total"] == 0
        content, tampered = plugin._load_entry(SENDER, "diaries/20261023.md")
        assert not tampered and content.count("私人句。") == 2


async def test_search_old_external_partial_and_tampered_sources():
    async with World(config={}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(SENDER, "没有来源的旧记录", entry_name_hint="20261022")
        old = search_diaries(plugin, SENDER, "旧记录")["results"][0]
        assert old["evidence_type"] == "unknown" and old["source_status"] == "missing"

        def add_external(data):
            data["entries"].setdefault("20261023.md", {})["external"] = [
                {"source": "bridge", "ext_id": "ext-1", "at": "2026-10-23T20:30:00"}
            ]
            return data

        plugin._save_diary(
            SENDER,
            "桥程序提交的外部材料",
            entry_name_hint="20261023",
            separator_note="（外部·bridge）",
            provenance_mutator=add_external,
        )
        external = search_diaries(plugin, SENDER, "外部材料")["results"][0]
        assert external["evidence_type"] == "external" and external["source"] == "bridge"
        old_with_other_day_provenance = search_diaries(plugin, SENDER, "旧记录")["results"][0]
        assert old_with_other_day_provenance["evidence_type"] == "unknown"

        plugin._save_diary(
            SENDER, "来源记录无法对应的正文", entry_name_hint="20261024"
        )
        assert plugin.record_self_provenance(SENDER, "20261024", ["不存在的私人句"])
        partial = search_diaries(plugin, SENDER, "无法对应")["results"][0]
        assert partial["evidence_type"] == "unknown"
        assert partial["source_status"] == "partial"

        import zipfile
        path = plugin._diary_path(SENDER)
        with zipfile.ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries[plugin.PROVENANCE_ENTRY] = b'{"version": 1, "entries": {}}'
        plugin._write_zip_atomic(path, entries)
        tampered = search_diaries(plugin, SENDER, "外部材料")
        assert tampered["tampered"] is True
        assert tampered["results"][0]["evidence_type"] == "unknown"
        assert tampered["results"][0]["source_status"] == "tampered"


async def test_search_loads_provenance_once_and_encrypted_zip_behaves_same():
    async with World(config={
        "storage_encryption_enabled": True,
        "storage_encryption_passphrase": "correct horse battery staple",
    }) as world:
        plugin = world.refactor.plugin
        self_text = "我独自翻出了旧对话。"
        plugin._save_diary(SENDER, f"加密用户事实。{self_text}", origin="private", mode="full")
        plugin._save_diary(
            SENDER, "另一天用户事实", origin="private", mode="full", entry_name_hint="20261024"
        )
        assert plugin.record_self_provenance(SENDER, "20261023", [self_text])
        calls = 0
        original = plugin.load_provenance

        def counted(sender_id):
            nonlocal calls
            calls += 1
            return original(sender_id)

        plugin.load_provenance = counted
        assert search_diaries(plugin, SENDER, "翻出了")["total"] == 0
        private = search_diaries(plugin, SENDER, "翻出了", evidence_type="self")
        assert private["total"] == 1 and private["results"][0]["self_masked"]
        assert calls == 2


async def test_recall_and_model_tool_support_evidence_type_without_scope_changes():
    async with World(config={"use_persona": False}) as world:
        inst = world.refactor
        plugin = inst.plugin
        self_text = "我独自看了一下午云。"
        plugin._save_diary(SENDER, f"用户约定。{self_text}", origin="private", mode="full")
        assert plugin.record_self_provenance(SENDER, "20261023", [self_text])
        default = await inst.run_handler("recall", make_event("/回忆 看了一下午"))
        assert "未命中" in default[0]
        private = "\n".join(
            await inst.run_handler("recall", make_event("/回忆 私人随笔 看了一下午"))
        )
        assert "混合段落" in private and "〔她的私人随笔〕" in private
        all_text = "\n".join(
            await inst.run_handler("recall", make_event("/回忆 全部 看了一下午"))
        )
        assert "混合段落" in all_text
        tool = await plugin.memory_diary_search(
            make_event(""), "看了一下午", evidence_type="self"
        )
        assert "混合段落" in tool
        invalid = await plugin.memory_diary_search(
            make_event(""), "看了一下午", evidence_type="not-a-type"
        )
        assert "未命中" in invalid


async def test_web_search_validates_and_forwards_evidence_type():
    import json
    from astrbot.api.web import bind_request_context

    async with World(config={}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            SENDER,
            "她的私人生活",
            origin="private",
            mode="life",
            separator_note="（无新对话·她的日常随笔）",
        )
        file_key = plugin._diary_path(SENDER).stem.removeprefix("diary_")

        async def call(query):
            with bind_request_context(FakeWebRequest(query=query)):
                response = await plugin.web_search_diaries(file_key)
            body = getattr(response, "body", b"{}")
            return getattr(response, "status_code", None), json.loads(bytes(body).decode())

        status, invalid = await call({"evidence_type": "unexpected"})
        assert status == 400 and "无效" in str(invalid)
        status, private = await call({"q": "私人生活", "evidence_type": "self"})
        assert status == 200 and private["total"] == 1
        assert private["results"][0]["evidence_label"] == "AI 私人随笔"
        status, default = await call({"q": "私人生活"})
        assert status == 200 and default["total"] == 0


async def test_search_partial_provenance_still_masks_known_self_text():
    async with World(config={}) as world:
        plugin = world.refactor.plugin
        plugin._save_diary(
            SENDER, "用户事实。仍应遮蔽的私人句。", origin="private", mode="full"
        )
        assert plugin.record_self_provenance(
            SENDER, "20261023", ["仍应遮蔽的私人句。", "已经失配的私人句。"]
        )
        assert plugin.record_explicit_provenance(SENDER, "20261023", ["用户事实。"])
        result = search_diaries(plugin, SENDER, "用户事实")
        assert result["total"] == 1
        item = result["results"][0]
        assert item["evidence_type"] == "unknown" and item["source_status"] == "partial"
        assert item["self_masked"] is True and item["has_explicit"] is True
        assert "仍应遮蔽" not in item["snippet"]
        assert "〔她的私人随笔〕" in item["snippet"]
        assert search_diaries(plugin, SENDER, "仍应遮蔽")["total"] == 0


async def test_self_search_long_mixed_snippet_anchors_placeholder():
    async with World(config={}) as world:
        plugin = world.refactor.plugin
        self_text = "我独自翻出了旧对话。"
        plugin._save_diary(
            SENDER,
            "用户相关内容。" * 100 + self_text,
            origin="private",
            mode="full",
        )
        assert plugin.record_self_provenance(SENDER, "20261023", [self_text])
        result = search_diaries(plugin, SENDER, "翻出了", evidence_type="self")
        assert result["total"] == 1
        snippet = result["results"][0]["snippet"]
        assert "〔她的私人随笔〕" in snippet
        assert self_text not in snippet
        assert len(snippet) <= 242


async def test_memory_diary_search_empty_query_returns_guidance():
    async with World(config={"use_persona": False}) as world:
        plugin = world.refactor.plugin
        result = await plugin.memory_diary_search(make_event("查日记"))
        assert "query" in result
        assert "关键词" in result
        assert "evidence_type" in result
