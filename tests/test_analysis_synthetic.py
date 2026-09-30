import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import soundfile as sf

from bpm_tagging.analysis import AnalysisError, analyze_audio


class SyntheticTempoTests(unittest.TestCase):
    def _click_track(self, path: Path, bpm: float, duration: float = 14.0) -> None:
        sample_rate = 22050
        samples = np.zeros(int(sample_rate * duration), dtype=np.float32)
        for onset in np.arange(0.0, duration, 60.0 / bpm):
            start = int(onset * sample_rate)
            length = min(int(0.03 * sample_rate), len(samples) - start)
            if length > 0:
                samples[start : start + length] += np.hanning(length) * 0.9
        sf.write(path, samples, sample_rate)

    def test_regular_180_is_ambiguous_without_range_but_policy_selects_double(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "regular.wav"
            self._click_track(path, 180)
            general = analyze_audio(path, analysis_windows=3, window_duration=6.0)
            eurobeat = analyze_audio(path, preferred_range=(160, 200), analysis_windows=3, window_duration=6.0)
            self.assertIn(general.bpm, {89, 90, 178, 180})
            self.assertEqual(eurobeat.bpm, 178)
            self.assertFalse(eurobeat.ambiguous)
            self.assertIsNotNone(eurobeat.alternate_bpm)

    def test_short_and_silent_tracks_fail_cleanly(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            short = root / "short.wav"
            silent = root / "silent.wav"
            sf.write(short, np.zeros(1000, dtype=np.float32), 22050)
            sf.write(silent, np.zeros(22050 * 3, dtype=np.float32), 22050)
            with self.assertRaises(AnalysisError):
                analyze_audio(short)
            with self.assertRaises(AnalysisError):
                analyze_audio(silent)
