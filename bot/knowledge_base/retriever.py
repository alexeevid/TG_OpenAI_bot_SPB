from sqlalchemy import text
from bot.settings import load_settings
from bot.openai_helper import embed
_settings = load_settings()
async def retrieve_context(session, dialog_id:int, question:str, top_k:int|None=None):
    top_k = top_k or _settings.kb_top_k
    q_emb = (await embed([question]))[0]
    sql = text('''
        SELECT c.content, c.meta, d.path, (1 - (c.embedding <=> :qvec)) AS cos_sim
        FROM kb_chunks c
        JOIN kb_documents d ON d.id = c.document_id AND d.is_active
        JOIN dialog_kb_links l ON l.document_id = d.id AND l.dialog_id = :dialog_id
        ORDER BY c.embedding <=> :qvec
        LIMIT :k
    ''')
    rows = session.execute(sql, {'dialog_id': dialog_id, 'qvec': q_emb, 'k': top_k}).mappings().all()
    return [dict(r) for r in rows if r.get('cos_sim') is None or float(r.get('cos_sim')) >= 0.7]
