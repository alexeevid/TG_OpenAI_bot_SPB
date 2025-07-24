from bot.db.session import SessionLocal
from bot.db.models import Message
def track_message(chat_id, user_id, role, content, model, usage):
    with SessionLocal() as s:
        s.add(Message(chat_id=chat_id, user_id=user_id, role=role, content=content[:4000],
                      tokens_prompt=getattr(usage,'prompt_tokens',None),
                      tokens_completion=getattr(usage,'completion_tokens',None),
                      total_tokens=getattr(usage,'total_tokens',None),
                      model=model, cost_usd=None))
        s.commit()
