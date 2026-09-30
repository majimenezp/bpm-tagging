import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mutagen import MutagenError

from bpm_tagging.audio import AudioToolError, _run


class ErrorPathTests(unittest.TestCase):
    def test_ffmpeg_error_is_wrapped(self) -> None:
        failure = subprocess.CalledProcessError(1, ["ffmpeg"], stderr="decode failed")
        with patch("bpm_tagging.audio.subprocess.run", side_effect=failure), self.assertRaises(AudioToolError) as context:
            _run(["ffmpeg"])
        self.assertIn("decode failed", str(context.exception))

    def test_missing_tag_writer_is_reported_by_metadata_layer(self) -> None:
        from bpm_tagging.metadata import write_bpm

        with TemporaryDirectory() as temp:
            path = Path(temp) / "not-audio.opus"
            path.write_bytes(b"invalid")
            with self.assertRaises((MutagenError, OSError, ValueError)):
                write_bpm(path, 128)

    def test_analysis_error_is_explicit_when_no_candidates_exist(self) -> None:
        from bpm_tagging.analysis import AnalysisError, choose_tempo

        with self.assertRaises(AnalysisError):
            choose_tempo(128, {})
