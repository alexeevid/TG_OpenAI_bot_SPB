class ContextManager:
    def __init__(self):
        self.chat_docs = {}  # chat_id -> [paths]

    def set_docs(self, chat_id: int, docs: list[str]):
        self.chat_docs[chat_id] = docs

    def get_docs(self, chat_id: int) -> list[str]:
        return self.chat_docs.get(chat_id, [])

    def reset(self, chat_id: int):
        self.chat_docs.pop(chat_id, None)
