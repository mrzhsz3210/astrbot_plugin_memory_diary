# -*- coding: utf-8 -*-
"""Drive the plugin's ``session_waiter`` interactive flows without a live bot.

The plugin imports ``session_waiter`` from AstrBot at module import time and
uses it as a decorator inside command handlers.  Swapping that module global
for a scriptable fake lets tests drive the "reply with 1 / 全部 / 取消 / a file"
branches deterministically, while still running the real handler bodies.

Non-interactive fallbacks (``_SESSION_WAITER_OK = False``) are tested by
flipping the module global instead; both helpers are applied symmetrically to
the baseline and refactored module.
"""

from __future__ import annotations

import functools

from .fakes import make_event

REPLIES_ATTR = "_harness_session_replies"
INSTALLED_ATTR = "_harness_session_installed"


class FakeController:
    def __init__(self):
        self.kept = False
        self.stopped = False

    def stop(self, error=None):
        self.kept = False
        self.stopped = True

    def keep(self, timeout: float = 0, reset_timeout: bool = False):
        self.kept = True

    def get_history_chains(self):
        return []


def set_replies(module, replies) -> None:
    """Replace the reply script for the next interactive handler invocation.

    Each reply is a dict:
      {"text": "1"}                       -- plain text reply
      {"messages": [File(...)]}           -- reply carrying message components
      {"raise": "TimeoutError"}           -- simulate waiter timeout
      {"raise": "RuntimeError: boom"}     -- simulate any other exception
    """
    setattr(module, REPLIES_ATTR, list(replies or []))


def set_supported(module, ok: bool) -> None:
    module._SESSION_WAITER_OK = bool(ok)


def install(module) -> None:
    """Install the fake decorator once per plugin module."""
    if getattr(module, INSTALLED_ATTR, False):
        return
    setattr(module, REPLIES_ATTR, [])

    def fake_session_waiter(timeout: int = 30, record_history_chains: bool = False):
        def decorator(func):
            @functools.wraps(func)
            async def wrapper(event, *args, **kwargs):
                while True:
                    replies = getattr(module, REPLIES_ATTR)
                    reply = replies.pop(0) if replies else {"text": "取消"}
                    if "raise" in reply:
                        spec = str(reply["raise"])
                        name = spec.split(":", 1)[0].strip()
                        err_cls = {
                            "TimeoutError": TimeoutError,
                            "RuntimeError": RuntimeError,
                        }.get(name, RuntimeError)
                        raise err_cls(spec.split(":", 1)[1].strip() if ":" in spec else name)
                    controller = FakeController()
                    ev = make_event(
                        reply.get("text", ""),
                        group_id=event.get_group_id(),
                        sender_id=event.get_sender_id(),
                        sender_name=event.get_sender_name(),
                        self_id=event.get_self_id(),
                        umo=event.unified_msg_origin,
                        messages=reply.get("messages"),
                        captured_dir=event.captured_dir,
                    )
                    await func(controller, ev)
                    event.sent.extend(ev.sent)
                    event.captured_files.extend(ev.captured_files)
                    if not controller.kept:
                        break
                event.stop_event()

            return wrapper

        return decorator

    module.session_waiter = fake_session_waiter
    setattr(module, INSTALLED_ATTR, True)


async def collect(handler, event) -> list[str]:
    """Consume a command handler (async generator) and return all its output.

    Includes yielded plain results followed by messages the handler sent itself
    through ``event.send`` (interactive waiter replies).
    """
    texts: list[str] = []
    async for result in handler(event):
        texts.append(getattr(result, "text", str(result)))
    texts.extend(event.result_texts())
    return texts
