
class ContextManager:
    """ chat_id -> set(document_id) """
    def __init__(self):
        self.ctx = {}

    def reset(self, chat_id: int):
        self.ctx.pop(chat_id, None)

    def add(self, chat_id: int, document_id: int):
        self.ctx.setdefault(chat_id, set()).add(document_id)

    def get(self, chat_id: int):
        return list(self.ctx.get(chat_id, set()))
