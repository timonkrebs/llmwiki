import hashlib
import hmac
import mimetypes
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from config import settings

router = APIRouter(prefix="/v1/storage", tags=["storage"])

@router.get("/{key:path}")
async def get_storage_file(key: str, expires: int = 0, signature: str = ""):
    if not settings.LOCAL_STORAGE_DIR:
        raise HTTPException(status_code=404, detail="Local storage not configured")

    if not expires or not signature:
        raise HTTPException(status_code=403, detail="Missing signature or expiration")

    if int(time.time()) > expires:
        raise HTTPException(status_code=403, detail="Link expired")

    secret = settings.SUPABASE_JWT_SECRET or "local-dev-secret"
    message = f"{key}:{expires}".encode("utf-8")
    expected_signature = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    base_dir = Path(settings.LOCAL_STORAGE_DIR).resolve()
    file_path = (base_dir / key).resolve()

    # Ensure path traversal is not possible
    if not file_path.is_relative_to(base_dir):
        raise HTTPException(status_code=403, detail="Access denied")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    content_type, _ = mimetypes.guess_type(str(file_path))
    if not content_type:
        content_type = "application/octet-stream"

    return FileResponse(path=file_path, media_type=content_type)
