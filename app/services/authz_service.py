class AuthzService:
    def __init__(self, settings):
        if isinstance(settings.admin_user_ids, str):
            self.admins = set(settings.admin_user_ids.split(","))
        elif isinstance(settings.admin_user_ids, set):
            self.admins = settings.admin_user_ids
        else:
            self.admins = set()

    def is_allowed(self, user_id: int) -> bool:
        return not self.admins or str(user_id) in self.admins
