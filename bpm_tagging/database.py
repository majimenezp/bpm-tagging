from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import ALGORITHM_VERSION, SCHEMA_VERSION
from .models import FileFingerprint, ProcessingStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Single-writer SQLite state store with deterministic state transitions."""

    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path.expanduser().resolve()
        self.read_only = read_only
        if read_only:
            if not self.path.exists():
                raise FileNotFoundError(self.path)
            self.connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=30000")
        if not read_only:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.initialize()
        else:
            self._validate_version()

    def _validate_version(self) -> int:
        try:
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(f"base SQLite inválida o incompatible: {exc}") from exc
        if row is None:
            raise RuntimeError("base SQLite sin schema_version")
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"base SQLite schema_version={version} es más nueva que la soportada ({SCHEMA_VERSION})"
            )
        return version

    def initialize(self) -> None:
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is not None and int(row[0]) > SCHEMA_VERSION:
            raise RuntimeError(
                f"base SQLite schema_version={row[0]} es más nueva que la soportada ({SCHEMA_VERSION})"
            )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT,
                device INTEGER,
                inode INTEGER,
                format TEXT,
                duration REAL,
                bpm REAL,
                alternate_bpm REAL,
                quality_score REAL,
                confidence REAL,
                raw_bpm REAL,
                stability REAL,
                candidate_margin REAL,
                beat_count INTEGER,
                windows_analyzed INTEGER,
                analysis_coverage REAL,
                algorithm_version TEXT NOT NULL,
                algorithm_signature TEXT,
                status TEXT NOT NULL,
                error_message TEXT,
                existing_bpm REAL,
                bpm_source TEXT,
                analyzed_at TEXT,
                tag_written_at TEXT,
                verified_at TEXT
                ,CHECK(size >= 0)
                ,CHECK(mtime_ns >= 0)
                ,CHECK(status IN (
                    'analyzed', 'written', 'verified', 'skipped_existing_bpm',
                    'ambiguous', 'write_failed', 'verification_failed',
                    'changed_during_analysis', 'failed', 'success'
                ))
            )
            """
        )
        existing = {row[1] for row in self.connection.execute("PRAGMA table_info(tracks)")}
        additions = {
            "device": "INTEGER",
            "inode": "INTEGER",
            "quality_score": "REAL",
            "stability": "REAL",
            "candidate_margin": "REAL",
            "beat_count": "INTEGER",
            "windows_analyzed": "INTEGER",
            "analysis_coverage": "REAL",
            "algorithm_signature": "TEXT",
            "verified_at": "TEXT",
        }
        for name, definition in additions.items():
            if name not in existing:
                self.connection.execute(f"ALTER TABLE tracks ADD COLUMN {name} {definition}")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_tracks_signature ON tracks(algorithm_signature)")
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.connection.commit()

    def get(self, path: Path) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM tracks WHERE path = ?", (str(path.resolve()),)
        ).fetchone()

    @staticmethod
    def _value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
        return row[key] if key in row.keys() else default

    def should_process(
        self,
        fingerprint: FileFingerprint,
        *,
        algorithm_signature: str | None = None,
        force: bool = False,
        retry_failed: bool = False,
        verify_hashes: bool = False,
    ) -> tuple[bool, str]:
        if force:
            return True, "force"
        row = self.get(fingerprint.path)
        if row is None:
            return True, "new"
        stat_same = (
            self._value(row, "size") == fingerprint.size
            and self._value(row, "mtime_ns") == fingerprint.mtime_ns
            and self._value(row, "device") == fingerprint.device
            and self._value(row, "inode") == fingerprint.inode
            and self._value(row, "algorithm_signature") == algorithm_signature
        )
        if not stat_same:
            return True, "changed"
        if verify_hashes and fingerprint.sha256 is not None and self._value(row, "sha256") != fingerprint.sha256:
            return True, "changed"
        status = self._value(row, "status")
        if status in {
            ProcessingStatus.VERIFIED.value,
            ProcessingStatus.SKIPPED_EXISTING_BPM.value,
        }:
            return False, "sqlite"
        if status in {
            ProcessingStatus.FAILED.value,
            ProcessingStatus.WRITE_FAILED.value,
            ProcessingStatus.VERIFICATION_FAILED.value,
            ProcessingStatus.CHANGED_DURING_ANALYSIS.value,
        }:
            return (True, "retry_failed") if retry_failed else (False, "failed")
        return True, "reprocess_status"

    def save_track(self, fingerprint: FileFingerprint, **values: Any) -> None:
        """Write a complete row; incompatible result fields are cleared by status."""
        if self.read_only:
            raise RuntimeError("la base está abierta en modo lectura")
        status = str(values.get("status", ProcessingStatus.FAILED.value))
        if status == "success":
            status = ProcessingStatus.VERIFIED.value
        result_statuses = {
            ProcessingStatus.ANALYZED.value,
            ProcessingStatus.WRITTEN.value,
            ProcessingStatus.VERIFIED.value,
            ProcessingStatus.AMBIGUOUS.value,
        }
        has_result = status in result_statuses
        row_values: dict[str, Any] = {
            "path": str(fingerprint.path),
            "size": fingerprint.size,
            "mtime_ns": fingerprint.mtime_ns,
            "sha256": fingerprint.sha256,
            "device": fingerprint.device,
            "inode": fingerprint.inode,
            "format": values.get("format") if has_result or status == ProcessingStatus.SKIPPED_EXISTING_BPM.value else None,
            "duration": values.get("duration") if has_result else None,
            "bpm": values.get("bpm") if has_result or status == ProcessingStatus.SKIPPED_EXISTING_BPM.value else None,
            "alternate_bpm": values.get("alternate_bpm") if has_result else None,
            "quality_score": values.get("quality_score", values.get("confidence")) if has_result else None,
            "confidence": values.get("quality_score", values.get("confidence")) if has_result else None,
            "raw_bpm": values.get("raw_bpm") if has_result else None,
            "stability": values.get("stability") if has_result else None,
            "candidate_margin": values.get("candidate_margin") if has_result else None,
            "beat_count": values.get("beat_count") if has_result else None,
            "windows_analyzed": values.get("windows_analyzed") if has_result else None,
            "analysis_coverage": values.get("analysis_coverage") if has_result else None,
            "algorithm_version": ALGORITHM_VERSION,
            "algorithm_signature": values.get("algorithm_signature"),
            "status": status,
            "error_message": values.get("error_message"),
            "existing_bpm": values.get("existing_bpm") if status == ProcessingStatus.SKIPPED_EXISTING_BPM.value else None,
            "bpm_source": values.get("bpm_source") if status == ProcessingStatus.SKIPPED_EXISTING_BPM.value else ("detected" if has_result else None),
            "analyzed_at": values.get("analyzed_at") or utc_now(),
            "tag_written_at": values.get("tag_written_at") if status in {ProcessingStatus.WRITTEN.value, ProcessingStatus.VERIFIED.value} else None,
            "verified_at": values.get("verified_at") if status == ProcessingStatus.VERIFIED.value else None,
        }
        fields = list(row_values)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{field}=excluded.{field}" for field in fields if field != "path")
        with self.connection:
            self.connection.execute(
                f"INSERT INTO tracks ({', '.join(fields)}) VALUES ({placeholders}) "
                f"ON CONFLICT(path) DO UPDATE SET {updates}",
                [row_values[field] for field in fields],
            )

    def integrity_check(self) -> str:
        return str(self.connection.execute("PRAGMA integrity_check").fetchone()[0])

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
