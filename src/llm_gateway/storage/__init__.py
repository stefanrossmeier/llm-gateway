"""Local, durable, transaction-based gateway state."""

from .sqlite import StateStore, StoreError

__all__ = ["StateStore", "StoreError"]
