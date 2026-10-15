"""Isolated visual preview: no company database, credentials, or Drive calls.

Run manually: python -m uvicorn tests.preview_photo_import:preview --port 8779
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1] / "app"
preview = FastAPI()
preview.mount("/static", StaticFiles(directory=ROOT / "static"))


@preview.get("/", response_class=HTMLResponse)
def home():
    template = (ROOT / "templates/index.html").read_text(encoding="utf-8")
    start = template.index('<section class="panel span-12" data-workspace="journals" id="panel-photos"')
    section = template[start:template.index("</section>", start) + len("</section>")]
    return ('<!doctype html><html lang="zh-Hant"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<link rel="stylesheet" href="/static/app.css"><title>相簿匯入隔離預覽</title>'
            '<main class="shell"><p>隔離預覽：不連接公司資料庫或 Google Drive。</p>' + section + '</main>'
            '<script src="/static/photo_import.js"></script><script>'
            'function loadPhotoUploads(){};PhotoAlbumImport.configure({sites:[{id:47,name:"善捷47"},{id:76,name:"金駿76"}],'
            'actor:{role:"admin"},today:"2026-10-15",refresh:async()=>{},api:async()=>({status:"imported",backup_status:"saved"})});'
            '</script></html>')
