import logging

logger = logging.getLogger(__name__)

class KnowledgeBaseRetriever:
    def __init__(self, settings):
        self.settings = settings
        # здесь инициализация индекса / векторного поиска

    def retrieve(self, query, docs=None, passwords=None):
        """
        Извлекает релевантный контекст из выбранных документов.
        passwords — dict {path: password}
        """
        relevant_chunks = []
        for doc_path in docs or []:
            try:
                if doc_path.lower().endswith(".pdf") and passwords and doc_path in passwords:
                    text = self._extract_pdf_with_password(doc_path, passwords[doc_path])
                else:
                    text = self._extract_text(doc_path)
                chunks = self._split_into_chunks(text)
                # логика поиска релевантных чанков по запросу query
                relevant_chunks.extend(chunks)
            except Exception as e:
                logger.error(f"Ошибка при обработке {doc_path}: {e}")
        return relevant_chunks

    def _extract_text(self, path):
        """
        Заглушка — логика извлечения текста из документа.
        """
        return ""

    def _split_into_chunks(self, text, chunk_size=1000, overlap=200):
        """
        Делим текст на чанки.
        """
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunks.append(text[start:end])
            start += chunk_size - overlap
        return chunks

    def _extract_pdf_with_password(self, path, password):
        """
        Извлечение текста из PDF с паролем.
        """
        from PyPDF2 import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            reader.decrypt(password)
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
