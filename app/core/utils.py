from __future__ import annotations

from typing import Any, Optional


def split_by_tokens(text: str, max_tokens: int, model: str = "gpt-4o-mini") -> list[str]:
    """Best-effort splitter that does NOT require optional deps.

    We *try* to use `tiktoken` if installed. If it isn't available (e.g., minimal Railway image),
    we fall back to a rough character-based split.
    """
    try:
        import tiktoken  # optional

        enc = tiktoken.get_encoding("cl100k_base")
        tokens = enc.encode(text or "")
        return [enc.decode(tokens[i : i + max_tokens]) for i in range(0, len(tokens), max_tokens)]
    except Exception:
        # Fallback: 1 token ~ 4 chars (very rough), but safe and dependency-free.
        s = text or ""
        step = max(1, int(max_tokens) * 4)
        return [s[i : i + step] for i in range(0, len(s), step)]


def with_mode_prefix(context: Any, user_id: Optional[int], text: str) -> str:
    """Префиксует ЛЮБОЕ исходящее сообщение строкой [РЕЖИМ: ...].

    Используется в хендлерах, где отправляются сервисные/менюшные сообщения.
    Если режим не найден — считаем "professional".
    """
    try:
        from .response_modes import ensure_mode_prefix

        mode = "professional"
        if context is not None and user_id is not None:
            # PTB: bot_data доступен и через context.bot_data, и через context.application.bot_data
            bot_data = None
            try:
                bot_data = getattr(context, "bot_data", None)
            except Exception:
                bot_data = None
            if bot_data is None:
                try:
                    bot_data = getattr(getattr(context, "application", None), "bot_data", None)
                except Exception:
                    bot_data = None

            ds = None
            if isinstance(bot_data, dict):
                ds = bot_data.get("svc_dialog")
            if ds:
                settings = ds.get_active_settings(user_id) or {}
                mode = str(settings.get("mode") or mode)

        return ensure_mode_prefix(text or "", mode)
    except Exception:
        return text
