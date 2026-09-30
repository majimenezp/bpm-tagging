from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, cast

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TBPM
from mutagen.mp4 import MP4Tags

from .models import MetadataInfo

_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
ABSOLUTE_MIN_BPM = 1.0
ABSOLUTE_MAX_BPM = 999.0


@dataclass(frozen=True)
class MetadataSnapshot:
    format_name: str
    existing_bpm: float | None
    other_tags: tuple[tuple[str, str], ...]


def parse_bpm_value(value: Any) -> float | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            match = _NUMBER.search(str(value).replace(",", "."))
            if not match:
                return None
            number = float(match.group(0))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and ABSOLUTE_MIN_BPM <= number <= ABSOLUTE_MAX_BPM else None


def _tag_value(tags: Mapping[str, Any] | None, keys: tuple[str, ...]) -> float | None:
    if not tags:
        return None
    for key in keys:
        try:
            if key in tags:
                parsed = parse_bpm_value(tags[key])
                if parsed is not None:
                    return parsed
        except (KeyError, TypeError, ValueError):
            continue
    lowered = {str(key).lower(): value for key, value in tags.items()}
    for key in keys:
        parsed = parse_bpm_value(lowered.get(key.lower()))
        if parsed is not None:
            return parsed
    return None


def _bpm_keys(audio: Any) -> tuple[str, ...]:
    tags = audio.tags
    if isinstance(tags, ID3):
        return ("TBPM",)
    if isinstance(tags, MP4Tags):
        return ("tmpo",)
    return ("BPM", "bpm")


def _canonical(value: Any) -> str:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    return str(value)


def _other_tags(audio: Any) -> tuple[tuple[str, str], ...]:
    tags = audio.tags
    if not tags:
        return ()
    bpm_keys = {key.lower() for key in _bpm_keys(audio)}
    return tuple(
        sorted(
            (str(key), _canonical(value))
            for key, value in tags.items()
            if str(key).lower() not in bpm_keys
        )
    )


def snapshot_metadata(path: Path) -> MetadataSnapshot:
    audio = MutagenFile(path, easy=False)
    if audio is None:
        raise ValueError(f"mutagen no reconoce el archivo: {path}")
    bpm = _tag_value(cast(Mapping[str, Any] | None, audio.tags), ("TBPM", "BPM", "tmpo", "bpm", "©tmp"))
    return MetadataSnapshot(type(audio).__name__, bpm, _other_tags(audio))


def read_metadata(path: Path) -> MetadataInfo:
    snapshot = snapshot_metadata(path)
    return MetadataInfo(format_name=snapshot.format_name, existing_bpm=snapshot.existing_bpm)


def can_write_metadata(path: Path) -> str:
    audio = MutagenFile(path, easy=False)
    if audio is None:
        raise ValueError(f"mutagen no reconoce el archivo: {path}")
    if audio.tags is None and not hasattr(audio, "add_tags"):
        raise ValueError(f"mutagen no ofrece tags escribibles para: {path}")
    return type(audio).__name__


def write_bpm(path: Path, bpm: int) -> bool:
    if parse_bpm_value(bpm) is None:
        raise ValueError(f"BPM fuera de rango: {bpm}")
    audio = MutagenFile(path, easy=False)
    if audio is None:
        raise ValueError(f"mutagen no reconoce el archivo: {path}")
    if audio.tags is None:
        if not hasattr(audio, "add_tags"):
            raise ValueError(f"mutagen no ofrece tags escribibles para: {path}")
        audio.add_tags()
    tags = audio.tags
    if isinstance(tags, ID3):
        current = _tag_value(cast(Mapping[str, Any], tags), ("TBPM",))
        if current is not None and round(current) == bpm:
            return False
        tags.delall("TBPM")
        tags.add(TBPM(encoding=3, text=str(bpm)))
    elif isinstance(tags, MP4Tags):
        current = _tag_value(cast(Mapping[str, Any], tags), ("tmpo",))
        if current is not None and round(current) == bpm:
            return False
        tags["tmpo"] = [int(bpm)]
    else:
        current = _tag_value(cast(Mapping[str, Any], tags), ("BPM", "bpm"))
        if current is not None and round(current) == bpm:
            return False
        for key in tuple(tags):
            if str(key).lower() == "bpm":
                del tags[key]
        tags["BPM"] = str(bpm)
    audio.save()
    return True


def verify_metadata(path: Path, expected_bpm: int, before: MetadataSnapshot) -> None:
    after = snapshot_metadata(path)
    if after.existing_bpm is None or round(after.existing_bpm) != expected_bpm:
        raise ValueError(
            f"verificación BPM fallida: esperado={expected_bpm} obtenido={after.existing_bpm}"
        )
    if after.other_tags != before.other_tags:
        raise ValueError("verificación de metadata fallida: cambiaron tags distintos de BPM")
