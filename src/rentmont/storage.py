"""Single-process repository: independent atomic files per listing."""
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from .models import (MonitorState, ExtractionRecord, Listing, NotificationRecord,
                     NotificationStatus, RentalDetails, SourceRecord, TelegramDelivery)

LOG = logging.getLogger(__name__)
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
            LOG.info("Created monitor state in %s", directory)
        else:
            LOG.debug("Loaded monitor state from %s (initialized=%s)",
                      self.path, self.initialized)

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

    def save_extraction(self, ident: str, details: RentalDetails,
                        content_hash: str | None = None) -> None:
        self._write(self._listing_path(ident, "extraction.json"),
                    ExtractionRecord(details=details, extracted_at=now(), content_hash=content_hash))

    def get_notification(self, ident: str) -> NotificationRecord | None:
        return self._read(self._listing_path(ident, "notification.json"), NotificationRecord)

    def sent_notifications(self) -> list[tuple[str, NotificationRecord]]:
        records = []
        for directory in sorted(self.directory.iterdir()):
            if not re.fullmatch(r"[0-9]+", directory.name):
                continue
            record = self.get_notification(directory.name)
            if record is not None and record.status == NotificationStatus.SENT:
                records.append((directory.name, record))
        return records

    def mark_gone(self, ident: str, *, notified: bool = False) -> NotificationRecord:
        record = self.get_notification(ident)
        if record is None or record.status != NotificationStatus.SENT:
            raise StorageError("Cannot mark an unsent notification as gone")
        record = record.model_copy(update={"gone_at": record.gone_at or now(),
                                           "gone_notified": notified})
        self._write(self._listing_path(ident, "notification.json"), record)
        return record

    def mark_updated(self, ident: str, content_hash: str, response: dict) -> None:
        record = self.get_notification(ident)
        if record is None or record.status != NotificationStatus.SENT:
            raise StorageError("Cannot update an unsent notification")
        self._write(self._listing_path(ident, "notification.json"),
                    record.model_copy(update={"content_hash": content_hash,
                                              "telegram_response": response}))

    def mark_sent(self, ident: str, delivery: TelegramDelivery,
                  content_hash: str | None = None) -> None:
        self._write(self._listing_path(ident, "notification.json"),
                    NotificationRecord(status=NotificationStatus.SENT, sent_at=now(),
                                       content_hash=content_hash,
                                       chat_id=delivery.chat_id, message_ids=delivery.message_ids,
                                       telegram_method=delivery.method,
                                       telegram_response=delivery.response))

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
            LOG.debug("Exported %d listings to %s", len(listings), path)
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
            raise StorageError(f"Cannot read {path}: {exc}") from exc
        try:
            return model.model_validate_json(content)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, error['loc'])) or '<root>'}: {error['type']}"
                for error in exc.errors(include_input=False)
            )
            raise StorageError(f"Invalid data in {path}: {problems}; file was not reset") from exc

    def _write(self, path: Path, record: BaseModel) -> None:
        record = type(record).model_validate(record.model_dump())
        try:
            atomic_write(path, record.model_dump_json(indent=2).encode("utf-8"))
            LOG.debug("Saved %s", path)
        except OSError as exc:
            raise StorageError(f"Cannot save {path}: {exc}") from exc

    def _commit(self, state: MonitorState) -> None:
        self._write(self.path, state)
        self._state = state
