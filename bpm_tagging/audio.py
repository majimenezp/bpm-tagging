from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


class AudioToolError(RuntimeError):
    """ffprobe or ffmpeg could not inspect/decode the file."""


@dataclass(frozen=True)
class ProbeInfo:
    format_name: str
    duration: float
    codec: str | None
    sample_rate: int | None
    channels: int | None


def _run(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise AudioToolError(detail.strip() or "audio tool failed") from exc


def probe_audio(path: Path) -> ProbeInfo:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "format=format_name,duration:stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ]
    )
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams") or []
        if not streams:
            raise AudioToolError(f"no hay stream de audio decodificable: {path}")
        stream = streams[0]
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
        return ProbeInfo(
            format_name=str((payload.get("format") or {}).get("format_name") or path.suffix.lower().lstrip(".")),
            duration=duration,
            codec=stream.get("codec_name"),
            sample_rate=int(stream["sample_rate"]) if stream.get("sample_rate") else None,
            channels=int(stream["channels"]) if stream.get("channels") else None,
        )
    except AudioToolError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AudioToolError(f"ffprobe returned invalid JSON for {path}: {exc}") from exc


def decode_pcm_window(source: Path, *, start: float, duration: float, sample_rate: int) -> bytes:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-t",
        f"{max(0.1, duration):.3f}",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "pipe:1",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, timeout=300)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        raise AudioToolError(detail.strip() or "ffmpeg PCM decode failed") from exc
    return result.stdout


def decode_to_wav(source: Path, destination: Path, sample_rate: int) -> None:
    """Compatibility helper; production analysis uses bounded PCM pipes."""
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def validate_tools() -> dict[str, str]:
    versions: dict[str, str] = {}
    for binary in ("ffmpeg", "ffprobe"):
        result = _run([binary, "-version"], timeout=10)
        versions[binary] = (result.stdout or result.stderr).splitlines()[0]
    return versions
