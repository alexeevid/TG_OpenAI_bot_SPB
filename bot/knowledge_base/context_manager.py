from bot.db.session import SessionLocal
from bot.db.models import KbSession
class ContextManager:
    def __init__(self, session_factory=SessionLocal): self.sf = session_factory
    def set_selected_documents(self, chat_id: int, doc_ids: list[int]):
        ids_str = ",".join(map(str, doc_ids))
        with self.sf() as s:
            ks = s.get(KbSession, chat_id)
            if ks: ks.selected_documents = ids_str
            else: s.add(KbSession(chat_id=chat_id, selected_documents=ids_str))
            s.commit()
    def get_selected_documents(self, chat_id: int) -> list[int]:
        with self.sf() as s:
            ks = s.get(KbSession, chat_id)
            if not ks or not ks.selected_documents: return []
            return [int(x) for x in ks.selected_documents.split(",") if x.strip().isdigit()]
    def reset(self, chat_id: int):
        with self.sf() as s:
            ks = s.get(KbSession, chat_id)
            if ks:
                ks.selected_documents = None
                s.commit()
