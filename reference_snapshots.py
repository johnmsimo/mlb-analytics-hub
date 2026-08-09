"""Versioned, atomic snapshots for shared MLB reference data.

The durable worker is the only process that talks to the upstream FanGraphs-
compatible MLB and Baseball Savant loaders.  It publishes their completed
in-memory dictionaries to the Fly volume.  Web processes hydrate from this
file and keep the last valid snapshot when a refresh is absent or corrupt.

The format is deliberately dependency-free:

    magic line
    bounded JSON metadata line
    gzip-compressed canonical JSON payload

The metadata carries a SHA-256 digest of the uncompressed payload.  A writer
fsyncs a same-directory temporary file and atomically replaces the published
path, so readers can observe either the previous complete version or the next
complete version, never a partially written dataset.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
MAGIC = b"MLB-REFERENCE-SNAPSHOT-V1\n"
MAX_HEADER_BYTES = 64 * 1024
MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
DATASET_NAMES = (
    "fg_bat",
    "fg_pit",
    "sv_pit_xstats",
    "sv_bat_xstats",
    "sv_bat_statcast",
    "sv_arsenal_pct",
    "sv_arsenal_velo",
    "sv_pit_arsenal_stats",
    "sv_bat_arsenal_stats",
)


class ReferenceSnapshotError(RuntimeError):
    """The published reference snapshot is incomplete or invalid."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _file_signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ReferenceSnapshotStore:
    """Publish and cache one trusted reference-data snapshot file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._signature: tuple[int, int, int, int] | None = None
        self._snapshot: dict[str, Any] | None = None
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def _decode(self, path: Path | None = None) -> dict[str, Any]:
        source_path = path or self.path
        with source_path.open("rb") as handle:
            if handle.readline(len(MAGIC) + 1) != MAGIC:
                raise ReferenceSnapshotError("invalid snapshot magic")
            header_line = handle.readline(MAX_HEADER_BYTES + 1)
            if not header_line or len(header_line) > MAX_HEADER_BYTES:
                raise ReferenceSnapshotError("invalid snapshot metadata")
            try:
                header = json.loads(header_line)
            except (TypeError, ValueError) as exc:
                raise ReferenceSnapshotError("invalid snapshot metadata JSON") from exc
            compressed = handle.read()

        if int(header.get("schemaVersion") or 0) != SCHEMA_VERSION:
            raise ReferenceSnapshotError("unsupported snapshot schema")
        payload_bytes = int(header.get("payloadBytes") or -1)
        if payload_bytes < 0 or payload_bytes > MAX_PAYLOAD_BYTES:
            raise ReferenceSnapshotError("snapshot payload exceeds size limit")
        if len(compressed) > MAX_COMPRESSED_BYTES:
            raise ReferenceSnapshotError("compressed snapshot exceeds size limit")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as archive:
                raw = archive.read(payload_bytes + 1)
        except (OSError, EOFError) as exc:
            raise ReferenceSnapshotError("invalid compressed snapshot payload") from exc
        if len(raw) != payload_bytes:
            raise ReferenceSnapshotError("snapshot payload size mismatch")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != str(header.get("sha256") or ""):
            raise ReferenceSnapshotError("snapshot digest mismatch")
        version = f"r{SCHEMA_VERSION}-{digest[:16]}"
        if str(header.get("version") or "") != version:
            raise ReferenceSnapshotError("snapshot version mismatch")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ReferenceSnapshotError("invalid snapshot payload JSON") from exc
        if int(payload.get("schemaVersion") or 0) != SCHEMA_VERSION:
            raise ReferenceSnapshotError("payload schema mismatch")
        datasets = payload.get("datasets")
        if not isinstance(datasets, dict):
            raise ReferenceSnapshotError("snapshot datasets are missing")
        for name in DATASET_NAMES:
            if not isinstance(datasets.get(name), dict):
                raise ReferenceSnapshotError(f"snapshot dataset is missing: {name}")
        counts = {name: len(datasets[name]) for name in DATASET_NAMES}
        if dict(header.get("counts") or {}) != counts:
            raise ReferenceSnapshotError("snapshot dataset counts mismatch")

        return {
            "metadata": {
                "schemaVersion": SCHEMA_VERSION,
                "version": version,
                "sha256": digest,
                "generatedAt": header.get("generatedAt"),
                "effectiveDate": payload.get("effectiveDate"),
                "payloadBytes": len(raw),
                "compressedBytes": len(compressed),
                "counts": counts,
            },
            "datasets": datasets,
        }

    def load(self) -> dict[str, Any] | None:
        """Return the current valid version, retaining the last good copy."""
        with self._lock:
            try:
                signature = _file_signature(self.path)
            except FileNotFoundError:
                self._last_error = "snapshot_not_found"
                return self._snapshot
            except OSError as exc:
                self._last_error = f"snapshot_stat_failed:{type(exc).__name__}"
                return self._snapshot

            if signature == self._signature and self._snapshot is not None:
                return self._snapshot
            try:
                snapshot = self._decode()
            except (OSError, ReferenceSnapshotError) as exc:
                self._last_error = str(exc)[:200]
                return self._snapshot

            self._signature = signature
            self._snapshot = snapshot
            self._last_error = None
            return snapshot

    def publish(
        self,
        datasets: Mapping[str, Mapping[str, Any]],
        *,
        effective_date: str,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Atomically publish a complete version and return its metadata."""
        normalized: dict[str, dict[str, Any]] = {}
        for name in DATASET_NAMES:
            value = datasets.get(name)
            if not isinstance(value, Mapping):
                raise ReferenceSnapshotError(f"cannot publish missing dataset: {name}")
            normalized[name] = dict(value)

        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "effectiveDate": str(effective_date),
            "datasets": normalized,
        }
        raw = _canonical_json(payload)
        digest = hashlib.sha256(raw).hexdigest()
        version = f"r{SCHEMA_VERSION}-{digest[:16]}"
        counts = {name: len(value) for name, value in normalized.items()}
        header = {
            "schemaVersion": SCHEMA_VERSION,
            "version": version,
            "sha256": digest,
            "generatedAt": generated_at or _utc_now(),
            "payloadBytes": len(raw),
            "counts": counts,
        }

        with self._lock:
            current = self.load()
            if (
                current
                and current.get("metadata", {}).get("sha256") == digest
                and self._last_error is None
            ):
                return {**dict(current["metadata"]), "changed": False}

            self.path.parent.mkdir(parents=True, exist_ok=True)
            compressed = gzip.compress(raw, compresslevel=6, mtime=0)
            header_line = _canonical_json(header) + b"\n"
            if len(header_line) > MAX_HEADER_BYTES:
                raise ReferenceSnapshotError("snapshot metadata is too large")
            blob = MAGIC + header_line + compressed
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=str(self.path.parent),
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(blob)
                    handle.flush()
                    os.fsync(handle.fileno())
                verified = self._decode(Path(temp_name))
                if verified.get("metadata", {}).get("sha256") != digest:
                    raise ReferenceSnapshotError("temporary snapshot verification failed")
                os.replace(temp_name, self.path)
                try:
                    directory_fd = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass

            self._signature = None
            self._snapshot = None
            loaded = self.load()
            if not loaded or loaded.get("metadata", {}).get("sha256") != digest:
                raise ReferenceSnapshotError("published snapshot could not be verified")
            return {**dict(loaded["metadata"]), "changed": True}

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = self.load()
            metadata = dict((snapshot or {}).get("metadata") or {})
            return {
                "available": bool(snapshot),
                **metadata,
                "path": str(self.path),
                "lastError": self._last_error,
            }
