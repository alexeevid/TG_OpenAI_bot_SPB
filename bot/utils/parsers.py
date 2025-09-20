import tempfile, fitz
from docx import Document as DocxDocument
from pptx import Presentation
from openpyxl import load_workbook

def extract_text_from_bytes(data: bytes, mime: str):
    mime=(mime or '').lower()
    if 'pdf' in mime:
        with fitz.open(stream=data, filetype='pdf') as doc:
            return '\n'.join(p.get_text('text') for p in doc), {'type':'pdf'}
    if 'word' in mime or 'docx' in mime:
        with tempfile.NamedTemporaryFile(suffix='.docx') as tmp:
            tmp.write(data); tmp.flush(); d=DocxDocument(tmp.name); return '\n'.join(p.text for p in d.paragraphs), {'type':'docx'}
    if 'presentation' in mime or 'pptx' in mime:
        with tempfile.NamedTemporaryFile(suffix='.pptx') as tmp:
            tmp.write(data); tmp.flush(); prs=Presentation(tmp.name); buf=[]
            for s in prs.slides:
                for shp in s.shapes:
                    if hasattr(shp,'text'): buf.append(shp.text)
            return '\n'.join(buf), {'type':'pptx'}
    if 'spreadsheet' in mime or 'excel' in mime or 'xlsx' in mime:
        with tempfile.NamedTemporaryFile(suffix='.xlsx') as tmp:
            tmp.write(data); tmp.flush(); wb=load_workbook(tmp.name, read_only=True, data_only=True); buf=[]
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    vals=[str(v) if v is not None else '' for v in row]
                    if any(vals): buf.append(' | '.join(vals))
            return '\n'.join(buf), {'type':'xlsx'}
    try:
        return data.decode('utf-8','ignore'), {'type':'txt'}
    except:
        return '', {'type':'unknown'}
