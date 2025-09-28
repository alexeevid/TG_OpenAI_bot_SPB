
class AuthzService:
    def __init__(self, settings):
        self.admins = set((settings.admin_user_ids or "").split(",")) if settings.admin_user_ids else set()
        self.allowed = set((settings.allowed_user_ids or "").split(",")) if settings.allowed_user_ids else set()
    def is_admin(self, tg_id: int|str) -> bool:
        return str(tg_id) in self.admins
    def is_allowed(self, tg_id: int|str) -> bool:
        return not self.allowed or str(tg_id) in self.allowed
