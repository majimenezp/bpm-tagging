from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

SUPPORTED_EXTENSIONS = frozenset({".opus", ".ogg", ".flac", ".mp3", ".m4a", ".wav"})
ALGORITHM_VERSION = "librosa-beat-v2"
SCHEMA_VERSION = 2
DEFAULT_DATABASE = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
) / "bpm-tagging" / "state.sqlite3"
ANALYSIS_SAMPLE_RATE = 22_050
ANALYSIS_HOP_LENGTH = 512
DEFAULT_MIN_BPM = 40
DEFAULT_MAX_BPM = 240
DEFAULT_ANALYSIS_WINDOWS = 3
DEFAULT_WINDOW_DURATION = 45.0
DEFAULT_QUEUE_SIZE = 0  # 0 means workers * 2.
MAX_FULL_ANALYSIS_SECONDS = 15 * 60


def _tool_version(binary: str) -> str:
    try:
        result = subprocess.run(
            [binary, "-version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"{binary} no está disponible o no responde: {exc}") from exc
    first_line = (result.stdout or result.stderr).splitlines()
    return first_line[0].strip() if first_line else "unknown"


def algorithm_signature(
    *,
    min_bpm: int = DEFAULT_MIN_BPM,
    max_bpm: int = DEFAULT_MAX_BPM,
    preferred_range: tuple[int, int] | None = None,
    analysis_windows: int = DEFAULT_ANALYSIS_WINDOWS,
    window_duration: float = DEFAULT_WINDOW_DURATION,
    full_analysis: bool = False,
    librosa_version: str | None = None,
    ffmpeg_version: str | None = None,
    ffprobe_version: str | None = None,
) -> str:
    """Return a deterministic signature for all result-affecting parameters."""
    if librosa_version is None:
        try:
            import librosa

            librosa_version = getattr(librosa, "__version__", "unknown")
        except ImportError:
            librosa_version = "unavailable"
    payload = {
        "logical": ALGORITHM_VERSION,
        "librosa": librosa_version,
        "ffmpeg": ffmpeg_version or _tool_version("ffmpeg"),
        "ffprobe": ffprobe_version or _tool_version("ffprobe"),
        "sample_rate": ANALYSIS_SAMPLE_RATE,
        "hop_length": ANALYSIS_HOP_LENGTH,
        "min_bpm": min_bpm,
        "max_bpm": max_bpm,
        "preferred_range": preferred_range,
        "analysis_windows": analysis_windows,
        "window_duration": window_duration,
        "full_analysis": full_analysis,
        "selector": "tempogram-autocorrelation-v2",
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_tools() -> dict[str, str]:
    versions = {name: _tool_version(name) for name in ("ffmpeg", "ffprobe")}
    try:
        import librosa
    except ImportError as exc:
        raise RuntimeError("librosa no está instalado en el entorno activo") from exc
    versions["librosa"] = getattr(librosa, "__version__", "unknown")
    return versions
