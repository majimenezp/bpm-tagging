import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bpm_tagging.discovery import discover_files, iter_discovery, normalize_extensions


class DiscoveryTests(unittest.TestCase):
    def test_recursive_discovery_ignores_checksum_and_unsupported(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "nested").mkdir()
            (root / "one.OPUS").write_bytes(b"x")
            (root / "nested" / "two.flac").write_bytes(b"x")
            (root / "checksum.md5").write_text("x")
            (root / "image.jpg").write_bytes(b"x")
            self.assertEqual([path.relative_to(root).as_posix() for path in discover_files(root)], ["nested/two.flac", "one.OPUS"])

    def test_extensions_are_case_insensitive_and_support_commas(self) -> None:
        self.assertEqual(normalize_extensions(["opus,.MP3", " flac "]), {".opus", ".mp3", ".flac"})

    def test_symlinks_are_rejected_and_external_target_is_not_discovered(self) -> None:
        with TemporaryDirectory() as temp, TemporaryDirectory() as outside_temp:
            root = Path(temp)
            outside = Path(outside_temp) / "external.opus"
            outside.write_bytes(b"must-not-be-touched")
            (root / "external.opus").symlink_to(outside)
            (root / "internal.opus").write_bytes(b"ok")
            files, issues, resolved = iter_discovery(root)
            self.assertEqual(files, [resolved / "internal.opus"])
            self.assertTrue(any(issue.reason == "symlink_rejected" for issue in issues))
            self.assertEqual(outside.read_bytes(), b"must-not-be-touched")

    def test_allow_symlinks_still_confines_resolved_target(self) -> None:
        with TemporaryDirectory() as temp, TemporaryDirectory() as outside_temp:
            root = Path(temp)
            outside = Path(outside_temp) / "external.flac"
            outside.write_bytes(b"outside")
            (root / "external.flac").symlink_to(outside)
            files, issues, _ = iter_discovery(root, allow_symlinks=True)
            self.assertEqual(files, [])
            self.assertTrue(any(issue.reason == "symlink_outside_root" for issue in issues))
