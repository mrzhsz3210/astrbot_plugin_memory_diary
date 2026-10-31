# -*- coding: utf-8 -*-
"""Fakes for the AstrBot surfaces the plugin touches.

Kept deliberately small: only the attributes/methods the plugin calls are
implemented, but the *shapes* mirror AstrBot 4.x (async conversation manager,
async provider, MessageEventResult from ``plain_result``, real message
components for At/File, real ``PluginUploadFile`` for web uploads).
"""

from __future__ import annotations

import io
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import env

env.ensure_astrbot_importable()

from astrbot.core.agent.message import TextPart  # noqa: E402
from astrbot.core.message.components import At, File, Plain  # noqa: E402
from astrbot.core.message.message_event_result import MessageChain  # noqa: E402


# ---------------------------------------------------------------------------
# message results / events
# ---------------------------------------------------------------------------


@dataclass
class FakeResult:
    """Stand-in for ``MessageEventResult`` returned by ``event.plain_result``."""

    text: str = ""
    kind: str = "plain"

    def to_text(self) -> str:
        return self.text


class FakeEvent:
    """Minimal ``AstrMessageEvent`` implementation.

    ``sent`` records everything the plugin sends during the handler call;
    ``captured_files`` receives copies of files sent as ``File`` components so
    tests can drive an export -> import round trip.
    """

    def __init__(
        self,
        message_str: str = "",
        *,
        sender_id: str = "10001",
        sender_name: str = "阿罗",
        group_id: str = "",
        self_id: str = "99999",
        umo: str = "",
        messages=None,
        captured_dir: Path | None = None,
    ):
        self.message_str = message_str
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self._self_id = self_id
        self.unified_msg_origin = umo or self._default_umo()
        self._messages = list(messages or [])
        self.sent: list[FakeResult] = []
        # v0.6 插图：记录每次 MessageChain 发送的组件类型名（如 ["Image"]），
        # 供 QQ 图文穿插测试断言"图片确实以 Image 组件发出、不是只有文字"。
        self.sent_chains: list[list[str]] = []
        self.captured_files: list[Path] = []
        self.captured_dir = captured_dir
        self.stopped = False
        self.bot = None  # forces the standard File-component send path

    # -- identity -----------------------------------------------------
    def _default_umo(self) -> str:
        if self._group_id:
            return f"aiocqhttp:GroupMessage:{self._group_id}"
        return f"aiocqhttp:FriendMessage:{self._sender_id}"

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_group_id(self) -> str:
        return self._group_id

    def get_self_id(self) -> str:
        return self._self_id

    def get_messages(self):
        return list(self._messages)

    def get_message_str(self) -> str:
        return self.message_str

    # -- results ------------------------------------------------------
    def plain_result(self, text) -> FakeResult:
        return FakeResult(text=str(text))

    def image_result(self, *args, **kwargs) -> FakeResult:  # pragma: no cover
        return FakeResult("[image]", kind="image")

    async def send(self, result) -> None:
        if isinstance(result, FakeResult):
            self.sent.append(result)
            return
        if isinstance(result, MessageChain):
            self.sent_chains.append([type(comp).__name__ for comp in result.chain])
            texts = []
            for comp in result.chain:
                if isinstance(comp, Plain):
                    texts.append(comp.text)
                elif isinstance(comp, File):
                    path = comp.file or ""
                    if path:
                        self._capture_file(Path(path))
            self.sent.append(FakeResult("\n".join(texts), kind="chain"))
            return
        # MessageEventResult from astrbot
        chain = getattr(result, "chain", None)
        if chain:
            texts = [getattr(c, "text", "") for c in chain if isinstance(c, Plain)]
            self.sent.append(FakeResult("".join(texts)))
            for comp in chain:
                if isinstance(comp, File) and comp.file:
                    self._capture_file(Path(comp.file))
            return
        self.sent.append(FakeResult(str(result)))

    def _capture_file(self, path: Path) -> None:
        dest_dir = self.captured_dir or (path.parent / "_captured")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        try:
            shutil.copy2(path, dest)
        except Exception:
            return
        self.captured_files.append(dest)

    def stop_event(self) -> None:
        self.stopped = True

    # -- convenience --------------------------------------------------
    def result_texts(self) -> list[str]:
        return [r.text for r in self.sent]


def make_event(
    text: str,
    *,
    group_id: str = "",
    sender_id: str = "10001",
    sender_name: str = "阿罗",
    self_id: str = "99999",
    umo: str = "",
    messages=None,
    captured_dir: Path | None = None,
) -> FakeEvent:
    return FakeEvent(
        text,
        sender_id=sender_id,
        sender_name=sender_name,
        group_id=group_id,
        self_id=self_id,
        umo=umo,
        messages=messages,
        captured_dir=captured_dir,
    )


def at_me(self_id: str = "99999") -> At:
    return At(qq=self_id)


def at_other(qq: str = "12345") -> At:
    return At(qq=qq)


def file_component(path: Path, name: str | None = None) -> File:
    return File(name=name or path.name, file=str(path))


# ---------------------------------------------------------------------------
# provider
# ---------------------------------------------------------------------------


@dataclass
class FakeLLMResponse:
    completion_text: str = ""


@dataclass
class FakeProvider:
    """Scripted LLM provider.

    ``replies`` is consumed in order; once one reply is left it is reused, so
    the same provider can serve a diary write plus several persona notices.
    """

    replies: list[str] = field(default_factory=lambda: ["（测试日记正文）"])
    calls: list[dict] = field(default_factory=list)
    fail_with: str | None = None

    async def text_chat(self, prompt=None, system_prompt=None, **kwargs):
        self.calls.append(
            {
                "prompt": prompt or "",
                "system_prompt": system_prompt or "",
                "kwargs": {k: v for k, v in kwargs.items() if k == "contexts"},
            }
        )
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with)
        if not self.replies:
            return FakeLLMResponse("")
        if len(self.replies) > 1:
            text = self.replies.pop(0)
        else:
            text = self.replies[0]
        return FakeLLMResponse(text)

    # convenience for differential comparisons
    def call_digest(self) -> list[dict]:
        return [
            {
                "prompt_sha": _sha(c["prompt"]),
                "prompt_len": len(c["prompt"]),
                "system_sha": _sha(c["system_prompt"]),
                "system_len": len(c["system_prompt"]),
            }
            for c in self.calls
        ]


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# conversation / persona managers
# ---------------------------------------------------------------------------


@dataclass
class FakeConversation:
    cid: str = "cid-1"
    user_id: str = ""
    history: str = "[]"
    persona_id: str = ""


class FakeConversationManager:
    """Per-UMO current conversation, history stored as JSON like AstrBot."""

    def __init__(self):
        self._convs: dict[str, FakeConversation] = {}
        self._all: list[FakeConversation] = []

    def set(self, umo: str, history=None, cid: str = "cid-1", persona_id: str = ""):
        conv = FakeConversation(
            cid=cid,
            user_id=umo,
            history=json.dumps(history or [], ensure_ascii=False),
            persona_id=persona_id,
        )
        self._convs[umo] = conv
        self._all = [c for c in self._all if c.user_id != umo] + [conv]
        return conv

    async def get_curr_conversation_id(self, umo: str):
        conv = self._convs.get(umo)
        return conv.cid if conv else None

    async def get_conversation(self, umo: str, cid: str):
        conv = self._convs.get(umo)
        if conv is None or conv.cid != cid:
            return None
        return conv

    async def get_conversations(self):
        return list(self._all)


class FakePersonaManager:
    def __init__(self, default_prompt: str = "", by_id: dict | None = None):
        self.default_prompt = default_prompt
        self.by_id = dict(by_id or {})

    async def get_persona_v3_by_id(self, persona_id: str):
        prompt = self.by_id.get(persona_id)
        if prompt is None:
            return None
        return {"system_prompt": prompt}

    async def get_default_persona_v3(self, umo: str = ""):
        return {"prompt": self.default_prompt}


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------


class FakeContext:
    def __init__(
        self,
        *,
        provider: FakeProvider | None = None,
        conversation_manager: FakeConversationManager | None = None,
        persona_manager=None,
        captured_dir: Path | None = None,
    ):
        self.provider = provider
        self.conversation_manager = conversation_manager or FakeConversationManager()
        self.persona_manager = persona_manager
        self.registered_web_apis: list[tuple] = []
        self.sent_messages: list[tuple] = []
        self.captured_dir = captured_dir

    async def get_using_provider_async(self, umo=None):
        return self.provider

    async def send_message(self, session, message_chain) -> bool:
        texts = []
        for comp in getattr(message_chain, "chain", []):
            text = getattr(comp, "text", "")
            if text:
                texts.append(text)
        self.sent_messages.append((str(session), "\n".join(texts)))
        return True

    def register_web_api(self, route, view_handler, methods, desc) -> None:
        self.registered_web_apis.append((route, view_handler, list(methods), desc))


# ---------------------------------------------------------------------------
# web request / uploads
# ---------------------------------------------------------------------------


class FakeWebRequest:
    """Mimics ``astrbot.api.web.PluginRequest`` closely enough for the views."""

    def __init__(
        self,
        *,
        query: dict | None = None,
        payload=None,
        uploads: dict | None = None,
        username: str | None = None,
        client_host: str | None = "127.0.0.1",
        forwarded_for: str = "",
        method: str = "GET",
        path: str = "/",
    ):
        self.query = dict(query or {})
        self.payload = payload if payload is not None else {}
        self.uploads = dict(uploads or {})
        self.username = username
        # 与 astrbot.api.web.PluginRequest 保持一致：对外暴露 client_host，
        # 不再有 remote_addr（旧实现误用 remote_addr 的坑见 test_web）。
        self.client_host = client_host
        self.method = method
        self.path = path
        self.headers = {}
        if forwarded_for:
            self.headers["x-forwarded-for"] = forwarded_for

    async def json(self, default=None):
        if self.payload is None:
            return default
        return self.payload

    async def files(self):
        return self.uploads

    async def form(self):
        return {}


def make_upload(data: bytes, filename: str = "upload.bin", content_type: str = "application/zip"):
    """Build a real ``PluginUploadFile`` so isinstance() checks pass."""
    from starlette.datastructures import Headers
    from starlette.datastructures import UploadFile as StarletteUploadFile

    from astrbot.api.web import PluginUploadFile

    upload = StarletteUploadFile(
        file=io.BytesIO(data),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )
    return PluginUploadFile(upload)


__all__ = [
    "At",
    "FakeContext",
    "FakeConversation",
    "FakeConversationManager",
    "FakeEvent",
    "FakeLLMResponse",
    "FakePersonaManager",
    "FakeProvider",
    "FakeResult",
    "FakeWebRequest",
    "File",
    "MessageChain",
    "Plain",
    "TextPart",
    "at_me",
    "at_other",
    "file_component",
    "make_event",
    "make_upload",
]
