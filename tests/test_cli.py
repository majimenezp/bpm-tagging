import hashlib
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import soundfile as sf
from mutagen import File as MutagenFile


class CliTests(unittest.TestCase):
    def _write_click(self, path: Path) -> None:
        sample_rate = 22050
        duration = 8.0
        samples = np.zeros(int(sample_rate * duration), dtype=np.float32)
        for onset in np.arange(0.0, duration, 60.0 / 180):
            start = int(onset * sample_rate)
            length = min(int(0.03 * sample_rate), len(samples) - start)
            samples[start : start + length] += np.hanning(length) * 0.9
        sf.write(path, samples, sample_rate)

    def test_dry_run_has_no_sqlite_or_audio_effect(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "track.wav"
            database = root / "state.sqlite3"
            self._write_click(audio)
            before = hashlib.sha256(audio.read_bytes()).hexdigest()
            completed = subprocess.run(
                [sys.executable, "-m", "bpm_tagging", str(root), "--database", str(database), "--dry-run", "--workers", "1"],
                cwd=Path(__file__).parents[1], capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(database.exists())
            self.assertEqual(hashlib.sha256(audio.read_bytes()).hexdigest(), before)

    def test_second_execution_uses_sqlite_fast_path(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            audio = root / "track.wav"
            database = root / "state.sqlite3"
            self._write_click(audio)
            command = [
                sys.executable, "-m", "bpm_tagging", str(root), "--database", str(database),
                "--workers", "1", "--overwrite-existing-bpm", "--profile", "eurobeat",
            ]
            first = subprocess.run(command, cwd=Path(__file__).parents[1], capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIsNotNone(MutagenFile(audio).tags)
            second = subprocess.run(command, cwd=Path(__file__).parents[1], capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("reason=sqlite", second.stderr + second.stdout)
