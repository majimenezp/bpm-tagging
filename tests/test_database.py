import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bpm_tagging.constants import SCHEMA_VERSION
from bpm_tagging.database import Database
from bpm_tagging.discovery import fingerprint_file


class DatabaseTests(unittest.TestCase):
    def test_schema_upsert_and_incremental_decisions(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "track.opus"
            audio.write_bytes(b"audio")
            fingerprint = fingerprint_file(audio)
            with Database(root / "state.sqlite3") as database:
                self.assertEqual(database.should_process(fingerprint), (True, "new"))
                database.save_track(fingerprint, status="success", bpm=128, format="ogg/opus")
                self.assertEqual(database.should_process(fingerprint), (False, "sqlite"))
                audio.write_bytes(b"changed")
                changed = fingerprint_file(audio)
                self.assertEqual(database.should_process(changed), (True, "changed"))
                row = database.get(audio)
                self.assertEqual(row["bpm"], 128)

    def test_failed_rows_need_explicit_retry_when_unchanged(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "track.mp3"
            audio.write_bytes(b"audio")
            fingerprint = fingerprint_file(audio)
            with Database(root / "state.sqlite3") as database:
                database.save_track(fingerprint, status="failed", error_message="boom")
                self.assertEqual(database.should_process(fingerprint), (False, "failed"))
                self.assertEqual(database.should_process(fingerprint, retry_failed=True), (True, "retry_failed"))

    def test_fast_path_does_not_require_hash(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "track.flac"
            audio.write_bytes(b"audio")
            fingerprint = fingerprint_file(audio)
            with Database(root / "state.sqlite3") as database:
                database.save_track(fingerprint, status="verified", bpm=128, format="flac", algorithm_signature="sig")
                stat_only = fingerprint_file(audio, include_hash=False)
                self.assertEqual(
                    database.should_process(stat_only, algorithm_signature="sig"),
                    (False, "sqlite"),
                )

    def test_failed_transition_clears_old_result(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "track.ogg"
            audio.write_bytes(b"audio")
            fingerprint = fingerprint_file(audio)
            with Database(root / "state.sqlite3") as database:
                database.save_track(fingerprint, status="verified", bpm=128, confidence=0.9, format="ogg")
                database.save_track(fingerprint, status="failed", error_message="decode")
                row = database.get(audio)
                self.assertEqual(row["status"], "failed")
                self.assertIsNone(row["bpm"])
                self.assertIsNone(row["confidence"])
                self.assertEqual(database.integrity_check(), "ok")

    def test_v1_schema_migrates_without_losing_row(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "legacy.sqlite3"
            audio = root / "track.mp3"
            audio.write_bytes(b"audio")
            fingerprint = fingerprint_file(audio)
            import sqlite3

            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata VALUES ('schema_version', '1');
                CREATE TABLE tracks (
                    path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, format TEXT, duration REAL, bpm REAL,
                    alternate_bpm REAL, confidence REAL, raw_bpm REAL,
                    algorithm_version TEXT NOT NULL, status TEXT NOT NULL,
                    error_message TEXT, existing_bpm REAL, bpm_source TEXT,
                    analyzed_at TEXT, tag_written_at TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO tracks(path,size,mtime_ns,sha256,algorithm_version,status,bpm) VALUES(?,?,?,?,?,?,?)",
                (str(fingerprint.path), fingerprint.size, fingerprint.mtime_ns, fingerprint.sha256, "old", "success", 123),
            )
            connection.commit()
            connection.close()
            with Database(db_path) as database:
                self.assertEqual(database.get(audio)["bpm"], 123)
                self.assertEqual(database.connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], str(SCHEMA_VERSION))
                self.assertEqual(database.integrity_check(), "ok")
