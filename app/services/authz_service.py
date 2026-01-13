from __future__ import annotations

from typing import Optional


class AuthzService:
    """
    Правила доступа:
    1) Админ (ADMIN_USER_IDS) — всегда allowed (страховка от самоблокировки).
    2) Если подключён repo_access и в таблице access_entries есть хотя бы 1 запись:
       - allowed только те, у кого is_allowed=True, + админы.
    3) Иначе (DB пуст / repo_access не подключён):
       - если env allowlist пуст => доступ всем
       - если env allowlist не пуст => доступ только тем, кто в списке
       - + админы
    """

    def __init__(self, settings, repo_access: Optional[object] = None):
        # settings.admin_user_ids / allowed_user_ids бывают строкой или set
        if isinstance(settings.admin_user_ids, str):
            admins = set(settings.admin_user_ids.split(","))
        elif isinstance(settings.admin_user_ids, set):
            admins = settings.admin_user_ids
        else:
            admins = set()

        if isinstance(settings.allowed_user_ids, str):
            allowed = set(settings.allowed_user_ids.split(","))
        elif isinstance(settings.allowed_user_ids, set):
            allowed = settings.allowed_user_ids
        else:
            allowed = set()

        self.admins = {str(uid).strip() for uid in admins if str(uid).strip()}
        self.allowed_env = {str(uid).strip() for uid in allowed if str(uid).strip()}

        self.repo_access = repo_access

    def is_admin(self, user_id: int) -> bool:
        uid = str(user_id)

        # 1) ENV админы
        if uid in self.admins:
            return True

        # 2) DB админы (если repo_access подключён)
        if self.repo_access:
            try:
                entry = self.repo_access.get(user_id)
                return bool(entry and getattr(entry, "is_admin", False))
            except Exception:
                pass

        return False

    def _db_mode_enabled(self) -> bool:
        if not self.repo_access:
            return False
        try:
            return bool(self.repo_access.has_any_entries())
        except Exception:
            # если БД временно падает — не валим бота, откатываемся на ENV
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
                pass

        # 3) ENV allowlist
        return (not self.allowed_env) or (str(user_id) in self.allowed_env)
