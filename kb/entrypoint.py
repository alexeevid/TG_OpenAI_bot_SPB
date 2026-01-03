from __future__ import annotations

from app.kb.syncer import KbSyncer


def run_kb_sync(db, yandex_client, openai_client):
    """
    Единая точка входа, которую можно дергать по KB_SYNC_ENTRYPOINT
    (у вас переменная уже описана в README). :contentReference[oaicite:8]{index=8}
    """
    svc = KbSyncer(yandex_client=yandex_client, db=db, openai_client=openai_client)
    return svc.sync()
