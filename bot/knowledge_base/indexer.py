import os
import logging

logger = logging.getLogger(__name__)

class KnowledgeBaseIndexer:
    def __init__(self, settings):
        self.settings = settings
        # здесь инициализация клиента Яндекс.Диск или локального каталога

    def list_documents(self):
        """
        Возвращает список документов с указанием, защищены ли они паролем.
        """
        docs = self._get_docs_from_source()  # реализация получения списка файлов уже есть у тебя
        result = []
        for idx, doc in enumerate(docs):
            is_protected = doc.lower().endswith(".pdf") and self._is_pdf_protected(doc)
            result.append({
                "index": idx,
                "name": os.path.basename(doc),
                "path": doc,
                "protected": is_protected
            })
        return result

    def _get_docs_from_source(self):
        """
        Заглушка — здесь твоя логика получения списка файлов из БЗ.
        """
        return []

    def _is_pdf_protected(self, path):
        """
        Проверяет, защищён ли PDF паролем.
        """
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(path)
            return reader.is_encrypted
        except Exception as e:
            logger.error(f"Ошибка проверки PDF {path}: {e}")
            return False
