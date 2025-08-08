import requests
from bot.settings import load_settings
BASE='https://cloud-api.yandex.net/v1/disk'
HEADERS={'Authorization': f'OAuth {load_settings().yandex_disk_token}'}

def list_files(path:str):
    r=requests.get(f'{BASE}/resources', headers=HEADERS, params={'path':path,'limit':1000}, timeout=60); r.raise_for_status()
    items=r.json().get('_embedded',{}).get('items',[]); out=[]
    for it in items:
        if it.get('type')=='file':
            out.append({'path':it.get('path'),'name':it.get('name'),'mime':it.get('mime_type'),'size':it.get('size'),'etag':it.get('md5') or it.get('sha256')})
    return out

def download_to_bytes(path:str)->bytes:
    r=requests.get(f'{BASE}/resources/download', headers=HEADERS, params={'path':path}, timeout=60); r.raise_for_status()
    href=r.json().get('href'); r2=requests.get(href, timeout=120); r2.raise_for_status(); return r2.content
