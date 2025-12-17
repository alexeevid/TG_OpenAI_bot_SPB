"""Legacy compatibility layer.

Некоторые старые модули могли импортировать app.services.dialog_manager.
В целевой архитектуре используйте DialogService + dialogs.settings в БД.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from .dialog_service import DialogService


# Простейшие типы, чтобы старые импорты не падали.
@dataclass
class Dialog:
    user_id: int
    settings: Dict[str, Any] = field(default_factory=dict)


def get_current_dialog(user_id: int) -> Dialog:
    # Legacy: без DB
    return Dialog(user_id=user_id, settings={})


def update_dialog_settings(dialog: Dialog) -> None:
    # Legacy no-op
    return


def reset_dialog(user_id: int) -> None:
    return
