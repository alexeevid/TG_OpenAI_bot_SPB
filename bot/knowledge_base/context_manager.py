import json
from bot.db.session import SessionLocal
from bot.db.models import KbSession

class ContextManager:
    def get_selected_documents(self, chat_id: int) -> list[int]:
        with SessionLocal() as s:
            rec = s.get(KbSession, chat_id)
            if not rec or not rec.selected_documents:
                return []
            try:
                return json.loads(rec.selected_documents)
            except Exception:
                return []

    def set_selected_documents(self, chat_id: int, doc_ids: list[int]):
        with SessionLocal() as s:
            rec = s.get(KbSession, chat_id)
            payload = json.dumps(doc_ids, ensure_ascii=False)
            if not rec:
                rec = KbSession(chat_id=chat_id, selected_documents=payload)
                s.add(rec)
            else:
                rec.selected_documents = payload
            s.commit()

    def reset(self, chat_id: int):
        with SessionLocal() as s:
            rec = s.get(KbSession, chat_id)
            if rec:
                rec.selected_documents = "[]"
                s.commit()
