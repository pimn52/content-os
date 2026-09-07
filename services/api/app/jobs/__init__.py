"""Persistent local job-claiming primitives.

There is deliberately no polling daemon here.  A future runner can compose
these state transitions without making SQLite or lease details part of the
domain contract.
"""

from .store import JobStore

__all__ = ["JobStore"]
