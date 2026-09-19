from pathlib import Path
from typing import Iterator

from app.logging_conf import get_logger
from app.storage.base import StorageBackend

logger = get_logger(__name__)


class LocalFileStorage(StorageBackend):
    def __init__(self, root_dir: str):
        self.root = Path(root_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"invalid storage key (path traversal): {key}")
        return path

    def save(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        logger.debug("storage_save", key=key, size_bytes=len(data))
        return key

    def read(self, key: str) -> bytes:
        data = self._path(key).read_bytes()
        logger.debug("storage_read", key=key, size_bytes=len(data))
        return data

    def stream(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        logger.debug("storage_stream_started", key=key)
        with open(self._path(key), "rb") as f:
            while chunk := f.read(chunk_size):
                yield chunk

    def exists(self, key: str) -> bool:
        return self._path(key).exists()
