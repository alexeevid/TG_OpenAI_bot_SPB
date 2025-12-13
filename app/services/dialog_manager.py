"""Compatibility dialog manager.

This module exists to satisfy imports like:
    from ..services.dialog_manager import get_current_dialog, update_dialog_settings

The original repository structure (as observed from runtime tracebacks) does NOT
include app/services/dialog_manager.py, but some patched handlers expect it.

This implementation is intentionally minimal and in-memory to unblock startup.
If your project already has a DB-backed DialogService, you can later replace the
implementation to persist settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Any

# Simple in-memory storage per Telegram user_id
_DIALOGS: Dict[int, "Dialog"] = {}


@dataclass
class Dialog:
    """Minimal dialog representation with settings."""
    user_id: int
    settings: Dict[str, Any] = field(default_factory=dict)


def get_current_dialog(user_id: int) -> Dialog:
    """Return current dialog for user_id (creates one if missing)."""
    d = _DIALOGS.get(user_id)
    if d is None:
        d = Dialog(user_id=user_id, settings={})
        _DIALOGS[user_id] = d
    return d


def update_dialog_settings(dialog: Dialog) -> None:
    """Persist dialog settings (in-memory)."""
    _DIALOGS[dialog.user_id] = dialog


def reset_dialog(user_id: int) -> None:
    """Optional helper to reset user's dialog settings."""
    _DIALOGS.pop(user_id, None)
