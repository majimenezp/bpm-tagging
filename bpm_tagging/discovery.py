from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .constants import SUPPORTED_EXTENSIONS
from .models import FileFingerprint


@dataclass(frozen=True)
class DiscoveryIssue:
    path: Path
    reason: str


def normalize_extensions(values: Iterable[str] | None) -> frozenset[str]:
    if values is None:
        return SUPPORTED_EXTENSIONS
    result: set[str] = set()
    for value in values:
        for item in value.split(","):
            item = item.strip().lower()
            if item:
                result.add(item if item.startswith(".") else f".{item}")
    return frozenset(result)


def is_confined(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


def iter_discovery(
    root: Path,
    extensions: Iterable[str] | None = None,
    *,
    allow_symlinks: bool = False,
) -> tuple[list[Path], list[DiscoveryIssue], Path]:
    """Walk without following links by default and resolve the root once."""
    resolved_root = root.expanduser().resolve(strict=True)
    if not resolved_root.is_dir():
        raise NotADirectoryError(resolved_root)
    allowed = normalize_extensions(extensions)
    issues: list[DiscoveryIssue] = []
    files: list[Path] = []
    visited_directories: set[tuple[int, int]] = set()

    def walk(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            issues.append(DiscoveryIssue(directory, f"unreadable_directory:{exc}"))
            return
        try:
            directory_stat = directory.stat(follow_symlinks=False)
            identity = (directory_stat.st_dev, directory_stat.st_ino)
            if identity in visited_directories:
                return
            visited_directories.add(identity)
            for entry in entries:
                candidate = Path(entry.path)
                try:
                    is_link = entry.is_symlink()
                except OSError as exc:
                    issues.append(DiscoveryIssue(candidate, f"stat_failed:{exc}"))
                    continue
                if is_link and not allow_symlinks:
                    issues.append(DiscoveryIssue(candidate, "symlink_rejected"))
                    continue
                if is_link and not is_confined(candidate, resolved_root):
                    issues.append(DiscoveryIssue(candidate, "symlink_outside_root"))
                    continue
                try:
                    if entry.is_dir(follow_symlinks=allow_symlinks):
                        if is_confined(candidate, resolved_root):
                            walk(candidate)
                        else:
                            issues.append(DiscoveryIssue(candidate, "path_outside_root"))
                    elif entry.is_file(follow_symlinks=allow_symlinks):
                        if not is_confined(candidate, resolved_root):
                            issues.append(DiscoveryIssue(candidate, "path_outside_root"))
                        elif candidate.suffix.lower() in allowed:
                            files.append(candidate)
                except OSError as exc:
                    issues.append(DiscoveryIssue(candidate, f"stat_failed:{exc}"))
        finally:
            del entries

    walk(resolved_root)
    files.sort(key=lambda path: str(path).casefold())
    return files, issues, resolved_root


def discover_files(
    root: Path,
    extensions: Iterable[str] | None = None,
    *,
    allow_symlinks: bool = False,
) -> list[Path]:
    files, _issues, _root = iter_discovery(root, extensions, allow_symlinks=allow_symlinks)
    return files


def stat_fingerprint(path: Path) -> FileFingerprint:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return FileFingerprint(
        resolved,
        stat.st_size,
        stat.st_mtime_ns,
        None,
        resolved.suffix.lower(),
        getattr(stat, "st_dev", None),
        getattr(stat, "st_ino", None),
    )


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_file(path: Path, *, include_hash: bool = True) -> FileFingerprint:
    fingerprint = stat_fingerprint(path)
    return FileFingerprint(
        fingerprint.path,
        fingerprint.size,
        fingerprint.mtime_ns,
        hash_file(fingerprint.path) if include_hash else None,
        fingerprint.extension,
        fingerprint.device,
        fingerprint.inode,
    )


def same_file_state(left: FileFingerprint, right: FileFingerprint) -> bool:
    return (
        left.path == right.path
        and left.size == right.size
        and left.mtime_ns == right.mtime_ns
        and left.device == right.device
        and left.inode == right.inode
        and (left.sha256 is None or right.sha256 is None or left.sha256 == right.sha256)
    )
