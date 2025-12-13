"""Compatibility dialog manager.

Provides symbols expected by handlers importing:
    from ..services.dialog_manager import get_current_dialog, update_dialog_settings

If your project already has a DB-backed dialog manager, you can replace this
module with an adapter to your persistence layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Any

_DIALOGS: Dict[int, "Dialog"] = {}


@dataclass
class Dialog:
    user_id: int
    settings: Dict[str, Any] = field(default_factory=dict)


def get_current_dialog(user_id: int) -> Dialog:
    d = _DIALOGS.get(user_id)
    if d is None:
        d = Dialog(user_id=user_id, settings={})
        _DIALOGS[user_id] = d
    return d


def update_dialog_settings(dialog: Dialog) -> None:
    _DIALOGS[dialog.user_id] = dialog


def reset_dialog(user_id: int) -> None:
    _DIALOGS.pop(user_id, None)
