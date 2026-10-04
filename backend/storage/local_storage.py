import aiofiles
from pathlib import Path
from fastapi import UploadFile
from backend.config import get_settings

settings = get_settings()


class LocalStorage:
    def __init__(self):
        self.upload_dir = Path(settings.upload_dir)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    async def save(self, file: UploadFile, filename: str) -> str:
        dest = self.upload_dir / filename
        async with aiofiles.open(dest, "wb") as f:
            while chunk := await file.read(1024 * 256):
                await f.write(chunk)
        return str(dest)

    def locate(self, key: str) -> str:
        """The identifier save_bytes() would return for this key (here, its path), so a
        caller that only knows the key can read or delete it later."""
        return str(self.upload_dir / key)

    def save_bytes(self, data: bytes, key: str) -> str:
        dest = self.upload_dir / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return str(dest)

    def read_bytes(self, file_path: str) -> bytes:
        return Path(file_path).read_bytes()

    def get_path(self, file_path: str) -> str:
        return file_path

    def delete(self, file_path: str) -> None:
        path = Path(file_path)
        if path.exists():
            path.unlink()

    def exists(self, file_path: str) -> bool:
        return Path(file_path).exists()
