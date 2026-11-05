# -*- coding: utf-8 -*-
"""Differential regression harness for astrbot_plugin_memory_diary.

The harness loads the untouched baseline plugin and the refactored working copy
into two independent Python package namespaces (``basepkg`` and ``newpkg``),
runs the same scenario against both with identical isolated ``ASTRBOT_ROOT``
directories and a frozen clock, then compares:

* the scenario return value (normalised),
* raised exception types / messages (normalised),
* every deterministic file the plugin wrote under its data directory
  (byte-for-byte after normalising absolute paths).

Entry point: ``python tests/run.py`` (see tests/README.md).
"""
