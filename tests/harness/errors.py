# -*- coding: utf-8 -*-
"""Shared exceptions for the standalone harness runner."""


class SkipTest(Exception):
    """Raised by a test to record an explicit skip (with a reason)."""
