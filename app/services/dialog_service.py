
from ..db.repo_dialogs import DialogsRepo

class DialogService:
    def __init__(self, repo: DialogsRepo | None):
        self._repo = repo
    def ensure_dialog(self, tg_user_id: str|int):
        if not self._repo:
            return type("D",(object,),{"id":0})()
        u = self._repo.ensure_user(str(tg_user_id))
        d = self._repo.new_dialog(u.id, title="")
        return d
    def add_user_message(self, dialog_id: int, text: str):
        if self._repo: self._repo.add_message(dialog_id, "user", text)
    def add_assistant_message(self, dialog_id: int, text: str):
        if self._repo: self._repo.add_message(dialog_id, "assistant", text)
