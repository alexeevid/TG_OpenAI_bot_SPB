
class ContextManager:
    """
    Very simple in-memory mapping: chat_id -> set of document ids/paths.
    """
    def __init__(self):
        self.ctx = {}

    def reset(self, chat_id: int):
        self.ctx.pop(chat_id, None)

    def add(self, chat_id: int, doc: str):
        self.ctx.setdefault(chat_id, set()).add(doc)

    def get(self, chat_id: int):
        return list(self.ctx.get(chat_id, set()))
