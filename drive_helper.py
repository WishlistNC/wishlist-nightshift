"""
Google Drive helper for Riley.
Reads live SOP documents from a shared Drive folder using a service account.
No human login required at runtime - this is server-to-server access.
"""
import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
import io
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
SOP_FOLDER_ID = os.environ.get("SOP_FOLDER_ID", "")

_drive_service = None

def get_drive_service():
    global _drive_service
    if _drive_service:
        return _drive_service
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        print("No Google service account configured")
        return None
    try:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        _drive_service = build('drive', 'v3', credentials=creds)
        return _drive_service
    except Exception as e:
        print(f"Error building Drive service: {e}")
        return None

def list_sop_files():
    service = get_drive_service()
    if not service or not SOP_FOLDER_ID:
        return []
    try:
        results = service.files().list(
            q=f"'{SOP_FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name, mimeType, modifiedTime)"
        ).execute()
        return results.get('files', [])
    except Exception as e:
        print(f"Error listing SOP files: {e}")
        return []

def read_doc_content(file_id, mime_type):
    service = get_drive_service()
    if not service:
        return ""
    try:
        if mime_type == 'application/vnd.google-apps.document':
            request = service.files().export_media(fileId=file_id, mimeType='text/plain')
        else:
            request = service.files().get_media(fileId=file_id)

        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"Error reading doc {file_id}: {e}")
        return ""

_sop_cache = {"content": "", "loaded_at": None}
CACHE_TTL_SECONDS = 300

def get_all_sop_content(force_refresh=False):
    import time
    now = time.time()
    if not force_refresh and _sop_cache["loaded_at"] and (now - _sop_cache["loaded_at"] < CACHE_TTL_SECONDS):
        return _sop_cache["content"]

    files = list_sop_files()
    combined = ""
    for f in files:
        name = f.get("name", "")
        mime = f.get("mimeType", "")
        if mime == 'application/vnd.google-apps.document':
            content = read_doc_content(f["id"], mime)
            combined += f"\n\n=== SOP DOCUMENT: {name} ===\n{content}\n"

    _sop_cache["content"] = combined
    _sop_cache["loaded_at"] = now
    return combined
