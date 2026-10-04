"""
Azure Blob Storage backend — same interface as LocalStorage.
Requires: pip install azure-storage-blob
Switch via env var: STORAGE_BACKEND=azure
"""
import io
from fastapi import UploadFile
from backend.config import get_settings

settings = get_settings()


class AzureStorage:
    def __init__(self):
        try:
            from azure.storage.blob import BlobServiceClient
            self._client = BlobServiceClient.from_connection_string(settings.azure_connection_string)
            self._container = settings.azure_container_name
            container_client = self._client.get_container_client(self._container)
            if not container_client.exists():
                container_client.create_container()
        except ImportError:
            raise RuntimeError(
                "azure-storage-blob is not installed. Run: pip install azure-storage-blob"
            )

    async def save(self, file: UploadFile, filename: str) -> str:
        data = await file.read()
        blob_client = self._client.get_blob_client(container=self._container, blob=filename)
        blob_client.upload_blob(io.BytesIO(data), overwrite=True)
        return filename  # blob key

    def save_bytes(self, data: bytes, key: str) -> str:
        blob_client = self._client.get_blob_client(container=self._container, blob=key)
        blob_client.upload_blob(io.BytesIO(data), overwrite=True)
        return key

    def read_bytes(self, file_path: str) -> bytes:
        blob_client = self._client.get_blob_client(container=self._container, blob=file_path)
        return blob_client.download_blob().readall()

    def get_path(self, file_path: str) -> str:
        blob_client = self._client.get_blob_client(container=self._container, blob=file_path)
        stream = blob_client.download_blob()
        import tempfile, os
        suffix = os.path.splitext(file_path)[1]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(stream.readall())
        tmp.flush()
        return tmp.name

    def delete(self, file_path: str) -> None:
        blob_client = self._client.get_blob_client(container=self._container, blob=file_path)
        blob_client.delete_blob(delete_snapshots="include")

    def exists(self, file_path: str) -> bool:
        blob_client = self._client.get_blob_client(container=self._container, blob=file_path)
        return blob_client.exists()
