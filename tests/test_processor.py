import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bpm_tagging.database import Database
from bpm_tagging.discovery import fingerprint_file
from bpm_tagging.models import AnalysisResult, MetadataInfo
from bpm_tagging.processor import ProcessingConfig, process_paths


def result() -> AnalysisResult:
    return AnalysisResult(
        duration=10.0,
        format_name="test",
        bpm=128,
        alternate_bpm=64,
        quality_score=0.9,
        raw_bpm=127.8,
        stability=0.95,
        candidate_margin=0.8,
        beat_count=20,
        windows_analyzed=1,
        analysis_coverage=1.0,
    )


class ProcessorSafetyTests(unittest.TestCase):
    def test_incremental_second_run_does_not_hash_unchanged_file(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "track.flac"
            audio.write_bytes(b"audio")
            fingerprint = fingerprint_file(audio)
            config = ProcessingConfig("test-signature", 40, 240, None, 1, 5.0, False, 0.0)
            with Database(root / "state.sqlite3") as database:
                database.save_track(
                    fingerprint,
                    status="verified",
                    bpm=128,
                    format="FLAC",
                    algorithm_signature=config.algorithm_signature,
                )
                with patch("bpm_tagging.processor.fingerprint_file", side_effect=AssertionError("hash must not run")):
                    counters, _ = process_paths(
                        [audio], database, workers=1, force=False, overwrite_existing_bpm=False,
                        retry_failed=False, config=config, authorized_root=root,
                    )
                self.assertEqual(counters.skipped_sqlite, 1)

    def test_change_between_analysis_and_write_is_not_written(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "track.opus"
            audio.write_bytes(b"original")
            config = ProcessingConfig("test-signature", 40, 240, None, 1, 5.0, False, 0.0)

            def analyze_and_change(path: Path, _config: ProcessingConfig) -> AnalysisResult:
                path.write_bytes(b"replaced")
                return result()

            with Database(root / "state.sqlite3") as database, patch(
                "bpm_tagging.processor.read_metadata",
                return_value=MetadataInfo("Test", None),
            ), patch("bpm_tagging.processor._analyze", side_effect=analyze_and_change), patch(
                "bpm_tagging.processor.write_bpm"
            ) as write:
                counters, interrupted = process_paths(
                    [audio], database, workers=1, force=False, overwrite_existing_bpm=True,
                    retry_failed=False, config=config, authorized_root=root,
                )
                self.assertFalse(interrupted)
                write.assert_not_called()
                self.assertEqual(counters.failed, 1)
                row = database.get(audio)
                self.assertEqual(row["status"], "changed_during_analysis")
                self.assertIsNone(row["bpm"])

    def test_write_failure_has_precise_state_and_no_stale_bpm(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "track.wav"
            audio.write_bytes(b"audio")
            config = ProcessingConfig("test-signature", 40, 240, None, 1, 5.0, False, 0.0)
            with Database(root / "state.sqlite3") as database, patch(
                "bpm_tagging.processor.read_metadata", return_value=MetadataInfo("Wave", None)
            ), patch("bpm_tagging.processor._analyze", return_value=result()), patch(
                "bpm_tagging.processor.snapshot_metadata", return_value=object()
            ), patch("bpm_tagging.processor.probe_audio", return_value=object()), patch(
                "bpm_tagging.processor.write_bpm", side_effect=OSError("read-only")
            ):
                counters, _ = process_paths(
                    [audio], database, workers=1, force=False, overwrite_existing_bpm=True,
                    retry_failed=False, config=config, authorized_root=root,
                )
                self.assertEqual(counters.failed, 1)
                self.assertEqual(database.get(audio)["status"], "write_failed")
                self.assertIsNone(database.get(audio)["bpm"])
