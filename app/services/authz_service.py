from __future__ import annotations

from typing import Optional

# repo_access передаём как зависимость (может быть None)
# чтобы не ломать текущую архитектуру


class AuthzService:
    """
    Правила доступа (в порядке приоритета):
    1) Админ (ADMIN_USER_IDS) — всегда allowed (страховка от самоблокировки).
    2) Если подключён repo_access и в DB есть хотя бы одна запись в access_entries:
       - allowed только те, у кого is_allowed=True, + любые админы.
    3) Иначе (DB пуст или repo_access не подключён):
       - если env allowlist пуст => доступ всем
       - если env allowlist не пуст => доступ только тем, кто в списке
       - + любые админы
    """

    def __init__(self, settings, repo_access: Optional[object] = None):
        self.settings = settings
        self.repo_access = repo_access

        # settings.admin_user_ids / allowed_user_ids уже Set[int] из settings.py
        self.admins = {str(int(x)) for x in (getattr(settings, "admin_user_ids", set()) or set())}
        self.allowed_env = {str(int(x)) for x in (getattr(settings, "allowed_user_ids", set()) or set())}

    def is_admin(self, user_id: int) -> bool:
        return str(user_id) in self.admins

    def _db_mode_enabled(self) -> bool:
        if not self.repo_access:
            return False
        try:
            return self.repo_access.has_any_entries()
        except Exception:
            # DB недоступна/ошибка — не валим бота, откатываемся на ENV
            return False

    def is_allowed(self, user_id: int) -> bool:
        # 1) админ всегда allowed
        if self.is_admin(user_id):
            return True

        # 2) DB ACL режим
        if self._db_mode_enabled():
            try:
                entry = self.repo_access.get(user_id)
                return bool(entry and entry.is_allowed)
            except Exception:
                # на ошибках БД — откат к ENV
                pass

        # 3) ENV allowlist
        return (not self.allowed_env) or (str(user_id) in self.allowed_env)
