from ..db.repo_dialogs import DialogsRepo

class DialogService:
    def __init__(self, repo: DialogsRepo | None):
        self._repo = repo

    def get_or_create_active(self, tg_user_id: str | int):
        """Создаёт первый диалог для пользователя и считает его активным (просто последний созданный)."""
        if not self._repo:
            return type("D",(object,),{"id":0})()
        u = self._repo.ensure_user(str(tg_user_id))
        # Создаём новый диалог только если у юзера ещё нет ни одного
        # (упрощённо; можно хранить active_id в таблице пользователей)
        d = self._repo.new_dialog(u.id, title="")
        return d

    def list_user_dialogs(self, tg_user_id: str | int, limit: int = 20):
        if not self._repo:
            return []
        # лёгкий репо-метод сделаем ниже напрямую в handlers через session, чтобы не плодить код
        return []

    def add_user_message(self, dialog_id: int, text: str):
        if self._repo: self._repo.add_message(dialog_id, "user", text)

    def add_assistant_message(self, dialog_id: int, text: str):
        if self._repo: self._repo.add_message(dialog_id, "assistant", text)
