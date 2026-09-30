import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bpm_tagging.audio import probe_audio
from bpm_tagging.metadata import snapshot_metadata, verify_metadata, write_bpm


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe no disponibles")
class FormatMatrixTests(unittest.TestCase):
    def test_real_metadata_write_preserves_non_bpm_tags_and_stream(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            specs = {
                ".opus": ["-c:a", "libopus"],
                ".ogg": ["-c:a", "libvorbis"],
                ".flac": ["-c:a", "flac"],
                ".mp3": ["-c:a", "libmp3lame", "-q:a", "5"],
                ".m4a": ["-c:a", "aac", "-b:a", "96k"],
                ".wav": ["-c:a", "pcm_s16le"],
            }
            for extension, codec_args in specs.items():
                path = root / f"synthetic{extension}"
                command = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    *codec_args, "-metadata", "title=metadata-preservation", str(path),
                ]
                subprocess.run(command, check=True, capture_output=True)
                before_probe = probe_audio(path)
                before_tags = snapshot_metadata(path)
                self.assertNotEqual(before_tags.existing_bpm, 128.0)
                write_bpm(path, 128)
                verify_metadata(path, 128, before_tags)
                after_probe = probe_audio(path)
                self.assertEqual(
                    (before_probe.codec, before_probe.sample_rate, before_probe.channels),
                    (after_probe.codec, after_probe.sample_rate, after_probe.channels),
                )
                self.assertLessEqual(abs(before_probe.duration - after_probe.duration), 0.05)
