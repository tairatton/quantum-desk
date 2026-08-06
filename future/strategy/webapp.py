"""Removed: the futures tree has no web dashboard.

This module is a deliberate compatibility stub. The former copied Flask app
depended on assets that do not belong to this tree, so importing it created a
dashboard that failed only when the first page was requested. Keeping the stub
makes that unsupported surface explicit without introducing a cross-tree
dependency.
"""
from __future__ import annotations


def create_app(*_args, **_kwargs):
    raise NotImplementedError(
        "the futures tree has no web dashboard; see the module docstring")
