import logging
from datetime import datetime
from typing import Dict, Iterable, Tuple, Any, Optional
import mimetypes

import yadisk
from sqlalchemy.orm import Session
from bot.db.models import Document

logger = logging.getLogger(__name__)


def _normalize_root(root_path: str) -> str:
    # Ожидаем ENV: "/База Знаний" (без 'disk:'). Здесь добавим префикс.
    root = root_path.strip()
    if root.startswith("disk:"):
        return root
    if not root.startswith("/"):
        root = "/" + root
    return f"disk:{root}"


def _guess_mime(name: str, fallback: str = "application/octet-stream") -> str:
    mt, _ = mimetypes.guess_type(name)
    return mt or fallback


def _iter_remote_files(y: yadisk.YaDisk, root: str) -> Iterable[Any]:
    """
    Безопасно итерируем все файлы под root (включая подкаталоги).
    Сначала пытаемся через walk, если недоступно — вручную рекурсией через listdir.
    """
    # Попытка через walk()
    try:
        for dir_meta in y.walk(root):
            # y.walk может возвращать dict-подобные объекты или Resource
            # Стараемся быть совместимыми
            try:
                files = dir_meta.files  # ResourceList
            except AttributeError:
                files = dir_meta.get("files", [])
            for f in files:
                # Берём только файлы
                rtype = getattr(f, "type", None) or getattr(f, "resource_type", None)
                if rtype == "file":
                    yield f
        return
    except Exception as e:
        logger.debug("y.walk() unavailable, fallback to recursive listdir: %s", e)

    # Fallback: ручной обход через listdir()
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            for it in y.listdir(cur):
                rtype = getattr(it, "type", None) or getattr(it, "resource_type", None)
                if rtype == "dir":
                    stack.append(getattr(it, "path", None) or f"{cur}/{getattr(it, 'name', '')}")
                elif rtype == "file":
                    yield it
        except Exception as e:
            logger.warning("listdir failed for %s: %s", cur, e)


def _file_meta(y: yadisk.YaDisk, item: Any) -> Dict[str, Any]:
    """
    Возвращает нормализованные метаданные по файлу.
    Гарантирует непустой 'sha256' (если нет — md5, если и его нет — суррогат).
    """
    path = getattr(item, "path", None)
    name = getattr(item, "name", None) or (path.rsplit("/", 1)[-1] if path else "file")
    mime = getattr(item, "mime_type", None) or _guess_mime(name)
    size = getattr(item, "size", 0) or 0
    sha256 = getattr(item, "sha256", None)
    md5 = getattr(item, "md5", None)
    modified = getattr(item, "modified", None)  # datetime или str

    # Если sha256 нет — попробуем дотянуть полные метаданные по конкретному пути
    if (not sha256) and path:
        try:
            m = y.get_meta(path)
            if isinstance(m, dict):
                sha256 = m.get("sha256") or sha256
                md5 = m.get("md5") or md5
                if not mime:
                    mime = m.get("mime_type") or mime
                if not size:
                    size = m.get("size") or size
                modified = m.get("modified") or modified
        except Exception:
            # молча продолжаем — ниже сделаем суррогат
            pass

    # Гарантируем непустой sha256
    digest = sha256 or md5
    if not digest:
        # суррогат: размер + отметка времени модификации
        try:
            ts = int(modified.timestamp()) if hasattr(modified, "timestamp") else None
        except Exception:
            ts = None
        digest = f"sz{size}-ts{ts or 0}"

    # Нормализуем дату
    if isinstance(modified, str):
        try:
            # Я.Диск отдаёт ISO-8601, например "2023-05-01T12:34:56+00:00"
            modified = datetime.fromisoformat(modified.replace("Z", "+00:00"))
        except Exception:
            modified = None

    return dict(
        path=path,
        title=name,
        mime=mime,
        size=int(size),
        sha256=str(digest),
        modified=modified,
    )


def sync_disk_to_db(db: Session, token: str, root_path: str) -> Dict[str, int]:
    """
    Полная синхронизация с Я.Диском:
    - проходит по всем файлам под root;
    - добавляет новые, обновляет изменившиеся, помечает удалённые;
    - возвращает статистику.

    Возвращает: {"added": N, "updated": N, "deleted": N, "unchanged": N}
    """
    root = _normalize_root(root_path)
    y = yadisk.YaDisk(token=token)

    # Собираем удалёнку
    remote: Dict[str, Dict[str, Any]] = {}
    for item in _iter_remote_files(y, root):
        meta = _file_meta(y, item)
        if not meta.get("path"):
            continue
        remote[meta["path"]] = meta

    # Читаем локальную БД
    db_docs = {d.path: d for d in db.query(Document).all()}

    added = updated = deleted = unchanged = 0
    seen_paths = set()

    # Upsert
    for rpath, rmeta in remote.items():
        seen_paths.add(rpath)
        doc = db_docs.get(rpath)
        if doc is None:
            # новый
            doc = Document(
                path=rmeta["path"],
                title=rmeta["title"],
                mime=rmeta["mime"],
                size=rmeta["size"],
                sha256=rmeta["sha256"],
                created_at=rmeta["modified"] or datetime.utcnow(),
            )
            db.add(doc)
            added += 1
        else:
            # сравниваем по sha256/size/mime/title
            changed = False
            if hasattr(doc, "sha256") and (doc.sha256 or "") != rmeta["sha256"]:
                doc.sha256 = rmeta["sha256"]
                changed = True
            if getattr(doc, "size", 0) != rmeta["size"]:
                doc.size = rmeta["size"]
                changed = True
            if getattr(doc, "mime", "") != rmeta["mime"]:
                doc.mime = rmeta["mime"]
                changed = True
            if getattr(doc, "title", "") != rmeta["title"]:
                doc.title = rmeta["title"]
                changed = True
            if changed:
                if hasattr(doc, "updated_at"):
                    doc.updated_at = datetime.utcnow()
                updated += 1
            else:
                unchanged += 1

    # Удалённые (в БД есть, на диске нет)
    for path, doc in db_docs.items():
        if path in seen_paths:
            continue
        # мягкое удаление, если есть такие поля; иначе — физически удаляем
        if hasattr(doc, "is_deleted"):
            setattr(doc, "is_deleted", True)
            if hasattr(doc, "deleted_at"):
                setattr(doc, "deleted_at", datetime.utcnow())
        else:
            db.delete(doc)
        deleted += 1

    db.commit()
    logger.info("KB sync: added=%d, updated=%d, deleted=%d, unchanged=%d", added, updated, deleted, unchanged)
    return {"added": added, "updated": updated, "deleted": deleted, "unchanged": unchanged}
