"""Single-process JSON storage. Invalid existing state is never reset silently."""
import os
from pathlib import Path
import tempfile

from pydantic import TypeAdapter

from .models import CachedExtraction, Database, Listing, RentalDetails, StoredAd


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
        return (ident in self._state.ads or ident in self._state.excluded_ids
                or ident in self._state.parsed_results)

    def pending(self) -> list[tuple[str, StoredAd]]:
        return [(ident, ad.model_copy(deep=True))
                for ident, ad in sorted(self._state.ads.items())
                if not ad.notified and not ad.listing.is_room_rent
                and ident not in self._state.excluded_ids]

    def get_ad(self, ident: str) -> StoredAd | None:
        ad = self._state.ads.get(ident)
        return ad.model_copy(deep=True) if ad is not None else None

    def get_extraction(self, ident: str) -> CachedExtraction | None:
        cached = self._state.parsed_results.get(ident)
        return cached.model_copy(deep=True) if cached is not None else None

    def cache_extraction(self, ident: str, fingerprint: str, result: RentalDetails) -> None:
        cached = CachedExtraction(fingerprint=fingerprint, result=result)
        if self._state.parsed_results.get(ident) == cached:
            return
        state = self._state.model_copy(deep=True)
        state.parsed_results[ident] = cached.model_copy(deep=True)
        self._commit(state)

    def add(self, ident: str, ad: StoredAd) -> None:
        if ident not in self._state.ads:
            self.save_ad(ident, ad)

    def save_ad(self, ident: str, ad: StoredAd) -> None:
        if self._state.ads.get(ident) == ad and ident not in self._state.excluded_ids:
            return
        state = self._state.model_copy(deep=True)
        state.ads[ident] = ad.model_copy(deep=True)
        if ident in state.excluded_ids:
            state.excluded_ids.remove(ident)
        self._commit(state)

    def exclude(self, ident: str) -> None:
        if ident in self._state.excluded_ids:
            return
        state = self._state.model_copy(deep=True)
        state.excluded_ids.append(ident)
        self._commit(state)

    def mark_notified(self, ident: str) -> None:
        state = self._state.model_copy(deep=True)
        state.ads[ident].notified = True
        self._commit(state)

    def mark_initialized(self) -> None:
        if self._state.initialized:
            return
        state = self._state.model_copy(deep=True)
        state.initialized = True
        self._commit(state)

    def export_listings(self, path: Path) -> None:
        listings = [ad.listing for ident, ad in sorted(self._state.ads.items())
                    if not ad.listing.is_room_rent and ident not in self._state.excluded_ids]
        atomic_write(path, TypeAdapter(list[Listing]).dump_json(listings, indent=2))

    def _commit(self, state: Database) -> None:
        try:
            atomic_write(self.path, state.model_dump_json(indent=2).encode("utf-8"))
        except OSError as exc:
            raise StorageError(f"Cannot save state to {self.path}") from exc
        # Keep memory unchanged if the write fails.
        self._state = state
