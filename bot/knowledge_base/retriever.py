import re

class KnowledgeBaseRetriever:
    def __init__(self, settings):
        self.settings = settings

    def retrieve(self, query: str, selected_docs: list, passwords: dict):
        all_chunks = []

        # Разбиваем запрос пользователя на слова (ключевые слова)
        query_words = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 2]

        # Берём текущие рабочие настройки
        max_chunks = 6   # было в твоей версии
        chunk_size = 800 # было в твоей версии

        for doc_path in selected_docs:
            text = self._load_and_parse_document(doc_path, passwords.get(doc_path))
            if not text:
                continue

            chunks = self._split_into_chunks(text, chunk_size=chunk_size)
            relevant = []

            for idx, chunk in enumerate(chunks):
                chunk_lower = chunk.lower()
                if any(word in chunk_lower for word in query_words):
                    relevant.append(chunk)
                    if idx + 1 < len(chunks):
                        relevant.append(chunks[idx + 1])

            # Если релевантных нет, берём первые чанки
            if not relevant:
                relevant = chunks[:2]

            all_chunks.extend(relevant)

        # Ограничиваем общее количество чанков
        if len(all_chunks) > max_chunks:
            all_chunks = all_chunks[:max_chunks]

        return all_chunks

    def _load_and_parse_document(self, path, password):
        # Здесь остаётся твоя текущая логика загрузки документа из Я.Диска
        ...

    def _split_into_chunks(self, text, chunk_size=800):
        words = text.split()
        chunks = []
        current_chunk = []
        current_len = 0

        for word in words:
            current_chunk.append(word)
            current_len += len(word) + 1
            if current_len >= chunk_size:
                chunks.append(" ".join(current_chunk))
                current_chunk = []
                current_len = 0

        if current_chunk:
            chunks.append(" ".join(current_chunk))

        return chunks
