"""Single-process JSON storage. Invalid existing state is never reset silently."""
import os
from pathlib import Path
import tempfile

from pydantic import TypeAdapter

from .models import Database, Listing, StoredAd


class StorageError(RuntimeError):
    pass


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same directory ensures replace stays on the same filesystem.
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            # Validation errors abort startup; never overwrite a corrupt database.
            self._state = Database.model_validate_json(path.read_bytes())
        else:
            self._state = Database(initialized=False, ads={})
            self._commit(self._state)

    @property
    def initialized(self) -> bool:
        return self._state.initialized

    def contains(self, ident: str) -> bool:
        return ident in self._state.ads

    def pending(self) -> list[tuple[str, StoredAd]]:
        return [(ident, ad.model_copy(deep=True))
                for ident, ad in sorted(self._state.ads.items()) if not ad.notified]

    def add(self, ident: str, ad: StoredAd) -> None:
        if self.contains(ident):
            return
        state = self._state.model_copy(deep=True)
        state.ads[ident] = ad.model_copy(deep=True)
        self._commit(state)

    def mark_notified(self, ident: str) -> None:
        state = self._state.model_copy(deep=True)
        state.ads[ident].notified = True
        self._commit(state)

    def mark_initialized(self) -> None:
        state = self._state.model_copy(deep=True)
        state.initialized = True
        self._commit(state)

    def export_listings(self, path: Path) -> None:
        listings = [ad.listing for _, ad in sorted(self._state.ads.items())]
        atomic_write(path, TypeAdapter(list[Listing]).dump_json(listings, indent=2))

    def _commit(self, state: Database) -> None:
        try:
            atomic_write(self.path, state.model_dump_json(indent=2).encode("utf-8"))
        except OSError as exc:
            raise StorageError(f"Cannot save state to {self.path}") from exc
        # Keep memory unchanged if the write fails.
        self._state = state
