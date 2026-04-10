import asyncio
import json
import logging
import os
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


class LocalStorageService:
    def __init__(self):
        self._base_dir = Path(settings.LOCAL_STORAGE_DIR)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _get_path(self, key: str) -> Path:
        return self._base_dir / key

    async def upload_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream"):
        path = self._get_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)

    async def upload_file(self, key: str, file_path: str, content_type: str = "application/octet-stream"):
        data = await asyncio.to_thread(Path(file_path).read_bytes)
        await self.upload_bytes(key, data, content_type)

    async def generate_presigned_get(self, key: str, expires_in: int = 3600) -> str:
        # For local storage, we serve files via a dedicated API route
        # Using settings.API_URL which defaults to http://localhost:8000
        return f"{settings.API_URL.rstrip('/')}/v1/storage/{key}"

    async def generate_presigned_put(self, key: str, content_type: str = "application/pdf", expires_in: int = 3600) -> str:
        raise NotImplementedError("Presigned PUT is not supported in local storage")

    async def download_bytes(self, key: str) -> bytes:
        path = self._get_path(key)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {key}")
        return await asyncio.to_thread(path.read_bytes)

    async def download_to_file(self, key: str, file_path: str):
        data = await self.download_bytes(key)
        await asyncio.to_thread(Path(file_path).write_bytes, data)

    async def download_json(self, key: str) -> dict:
        body = await self.download_bytes(key)
        return json.loads(body)
