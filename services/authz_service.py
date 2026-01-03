class AuthzService:
    def __init__(self, settings):
        # Поддержка строки, множества или None
        if isinstance(settings.admin_user_ids, str):
            self.admins = set(settings.admin_user_ids.split(","))
        elif isinstance(settings.admin_user_ids, set):
            self.admins = settings.admin_user_ids
        else:
            self.admins = set()

        if isinstance(settings.allowed_user_ids, str):
            self.allowed = set(settings.allowed_user_ids.split(","))
        elif isinstance(settings.allowed_user_ids, set):
            self.allowed = settings.allowed_user_ids
        else:
            self.allowed = set()

        # Очистим от пустых строк, приведём всё к str
        self.admins = {str(uid).strip() for uid in self.admins if str(uid).strip()}
        self.allowed = {str(uid).strip() for uid in self.allowed if str(uid).strip()}

    def is_allowed(self, user_id: int) -> bool:
        return not self.allowed or str(user_id) in self.allowed

    def is_admin(self, user_id: int) -> bool:
        return str(user_id) in self.admins
