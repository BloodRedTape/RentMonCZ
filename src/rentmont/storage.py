"""Single-process repository: independent atomic files per listing."""
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import tempfile
from typing import TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from .models import (MonitorState, ExtractionRecord, Listing, NotificationRecord,
                     NotificationStatus, RentalDetails, SourceRecord)

T = TypeVar("T", bound=BaseModel)


class StorageError(RuntimeError):
    pass


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def now() -> datetime:
    return datetime.now(timezone.utc)


class JsonStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "state.json"
        self._state = self._read(self.path, MonitorState)
        if self._state is None:
            self._state = MonitorState()
            self._write(self.path, self._state)

    @property
    def initialized(self) -> bool:
        return self._state.initialized

    @property
    def current_search_ids(self) -> frozenset[str]:
        return self._state.current_search_ids

    def set_current_search(self, ids: set[str]) -> None:
        for ident in ids:
            self._listing_path(ident, "source.json")
        self._commit(self._state.model_copy(update={"current_search_ids": frozenset(ids)}))

    def get_source(self, ident: str) -> SourceRecord | None:
        path = self._listing_path(ident, "source.json")
        record = self._read(path, SourceRecord)
        if record is not None and str(record.raw["hash_id"]) != ident:
            raise StorageError(f"Listing ID does not match directory: {path}")
        return record

    def save_source(self, raw: dict) -> None:
        ident = str(raw["hash_id"])
        old = self.get_source(ident)
        timestamp = now()
        record = SourceRecord(raw=raw, first_seen_at=old.first_seen_at if old else timestamp,
                              last_seen_at=timestamp)
        self._write(self._listing_path(ident, "source.json"), record)

    def get_extraction(self, ident: str) -> ExtractionRecord | None:
        return self._read(self._listing_path(ident, "extraction.json"), ExtractionRecord)

    def save_extraction(self, ident: str, details: RentalDetails) -> None:
        self._write(self._listing_path(ident, "extraction.json"),
                    ExtractionRecord(details=details, extracted_at=now()))

    def get_notification(self, ident: str) -> NotificationRecord | None:
        return self._read(self._listing_path(ident, "notification.json"), NotificationRecord)

    def mark_sent(self, ident: str) -> None:
        self._write(self._listing_path(ident, "notification.json"),
                    NotificationRecord(status=NotificationStatus.SENT, sent_at=now()))

    def suppress(self, ident: str) -> None:
        if self.get_notification(ident) is None:
            self._write(self._listing_path(ident, "notification.json"),
                        NotificationRecord(status=NotificationStatus.SUPPRESSED))

    def mark_initialized(self) -> None:
        if not self.initialized:
            self._commit(self._state.model_copy(update={"initialized": True}))

    def export_listings(self, path: Path, listings: list[Listing]) -> None:
        try:
            atomic_write(path, TypeAdapter(list[Listing]).dump_json(listings, indent=2))
        except OSError as exc:
            raise StorageError(f"Cannot export listings to {path}") from exc

    def _listing_path(self, ident: str, filename: str) -> Path:
        # Sreality IDs are numeric; never allow external IDs to escape DATA_DIR.
        if not re.fullmatch(r"[0-9]+", ident):
            raise StorageError("Expected a numeric Sreality ID")
        directory = self.directory / ident
        if directory.is_symlink():
            raise StorageError(f"Listing directory must not be a symlink: {directory}")
        return directory / filename

    def _read(self, path: Path, model: type[T]) -> T | None:
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError(f"Cannot read {path}") from exc
        try:
            return model.model_validate_json(content)
        except ValidationError as exc:
            raise StorageError(f"Invalid data in {path}; file was not reset") from exc

    def _write(self, path: Path, record: BaseModel) -> None:
        record = type(record).model_validate(record.model_dump())
        try:
            atomic_write(path, record.model_dump_json(indent=2).encode("utf-8"))
        except OSError as exc:
            raise StorageError(f"Cannot save {path}") from exc

    def _commit(self, state: MonitorState) -> None:
        self._write(self.path, state)
        self._state = state
