from abc import ABC, abstractmethod
from typing import Iterator


class StorageBackend(ABC):
    @abstractmethod
    def save(self, key: str, data: bytes) -> str: ...

    @abstractmethod
    def read(self, key: str) -> bytes: ...

    @abstractmethod
    def stream(self, key: str, chunk_size: int = 1024 * 1024) -> Iterator[bytes]: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...
