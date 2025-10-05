from sqlalchemy.orm import Session
from ..db.repo_dialogs import DialogsRepo
from ..db.models import Dialog, User

class DialogService:
    def __init__(self, repo: DialogsRepo | None):
        self._repo = repo

    def _sf(self):
        return self._repo.sf if self._repo else None

    def _get_user(self, tg_user_id: str | int):
        if not self._repo:
            return None
        return self._repo.ensure_user(str(tg_user_id))

    def get_last_dialog(self, tg_user_id: str | int):
        """Вернуть последний (по id) диалог пользователя, если есть."""
        if not self._repo:
            return type("D",(object,),{"id":0})()
        sf = self._sf()
        uid = str(tg_user_id)
        with sf() as s:  # type: Session
            u = s.query(User).filter_by(tg_id=uid).first()
            if not u:
                return None
            d = s.query(Dialog).filter(Dialog.user_id == u.id).order_by(Dialog.id.desc()).first()
            return d

    def get_or_create_active(self, tg_user_id: str | int):
        """
        Теперь: НЕ создаём новый диалог, если у пользователя уже есть.
        Возвращаем последний диалог; новый создаём только при полном отсутствии.
        """
        if not self._repo:
            return type("D",(object,),{"id":0})()
        last = self.get_last_dialog(tg_user_id)
        if last:
            return last
        u = self._get_user(tg_user_id)
        return self._repo.new_dialog(u.id, title="")

    def new_dialog(self, tg_user_id: str | int, title: str = ""):
        """Явно создать новый диалог (для /dialog_new и /reset)."""
        if not self._repo:
            return type("D",(object,),{"id":0})()
        u = self._get_user(tg_user_id)
        return self._repo.new_dialog(u.id, title=title)

    def add_user_message(self, dialog_id: int, text: str):
        if self._repo: self._repo.add_message(dialog_id, "user", text)

    def add_assistant_message(self, dialog_id: int, text: str):
        if self._repo: self._repo.add_message(dialog_id, "assistant", text)
