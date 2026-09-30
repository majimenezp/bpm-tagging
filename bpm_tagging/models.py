from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FileFingerprint:
    path: Path
    size: int
    mtime_ns: int
    sha256: str | None
    extension: str
    device: int | None = None
    inode: int | None = None


class ProcessingStatus(StrEnum):
    ANALYZED = "analyzed"
    WRITTEN = "written"
    VERIFIED = "verified"
    SKIPPED_EXISTING_BPM = "skipped_existing_bpm"
    AMBIGUOUS = "ambiguous"
    WRITE_FAILED = "write_failed"
    VERIFICATION_FAILED = "verification_failed"
    CHANGED_DURING_ANALYSIS = "changed_during_analysis"
    FAILED = "failed"


@dataclass(frozen=True)
class MetadataInfo:
    format_name: str
    existing_bpm: float | None


@dataclass(frozen=True)
class AnalysisResult:
    duration: float
    format_name: str
    bpm: int
    alternate_bpm: int | None
    quality_score: float
    raw_bpm: float
    stability: float
    candidate_margin: float
    beat_count: int
    windows_analyzed: int
    analysis_coverage: float
    ambiguous: bool = False

    @property
    def confidence(self) -> float:
        """Compatibility alias; callers should use quality_score."""
        return self.quality_score


@dataclass(frozen=True)
class ProcessingOutcome:
    status: str
    error_message: str | None = None
    modified: bool = False
    result: AnalysisResult | None = None


def row_to_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
