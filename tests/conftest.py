# -*- coding: utf-8 -*-
"""Optional pytest bootstrap.

The suite is designed to run without pytest via ``python tests/run.py`` on the
AstrBot launcher venv (which does not ship pytest).  If pytest is installed in
an environment, this conftest makes ``import harness`` work and points pytest at
the AstrBot core checkout.  Async tests additionally require ``pytest-asyncio``
with ``asyncio_mode = auto`` (see ``tests/pytest.ini``); the standalone runner
needs no plugins at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from harness import env  # noqa: E402

env.isolate_astrbot_root()
env.ensure_astrbot_importable()
