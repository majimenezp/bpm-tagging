from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .audio import decode_pcm_window, probe_audio
from .constants import (
    ANALYSIS_HOP_LENGTH,
    ANALYSIS_SAMPLE_RATE,
    DEFAULT_ANALYSIS_WINDOWS,
    DEFAULT_MAX_BPM,
    DEFAULT_MIN_BPM,
    DEFAULT_WINDOW_DURATION,
    MAX_FULL_ANALYSIS_SECONDS,
)
from .models import AnalysisResult


class AnalysisError(RuntimeError):
    """Audio was readable but no usable tempo evidence was available."""


@dataclass(frozen=True)
class TempoDecision:
    bpm: int
    alternate_bpm: int | None
    candidate_margin: float
    ambiguous: bool


def tempo_candidates(
    raw_bpm: float,
    minimum: int = DEFAULT_MIN_BPM,
    maximum: int = DEFAULT_MAX_BPM,
) -> list[int]:
    candidates = {round(raw_bpm), round(raw_bpm / 2), round(raw_bpm * 2)}
    return sorted(candidate for candidate in candidates if minimum <= candidate <= maximum)


def _preferred_bonus(candidate: int, preferred_range: tuple[int, int] | None) -> float:
    if preferred_range is None:
        return 0.0
    low, high = preferred_range
    # This is an explicit user policy, not a genre-independent prior.  A profile
    # is allowed to resolve an otherwise plausible half/double-time pair.
    return 1.0 if low <= candidate <= high else 0.0


def choose_tempo_detailed(
    raw_bpm: float,
    candidate_scores: dict[int, float],
    *,
    preferred_range: tuple[int, int] | None = None,
    ambiguity_margin: float = 0.08,
) -> TempoDecision:
    candidates = {
        int(round(candidate)): float(score)
        for candidate, score in candidate_scores.items()
        if math.isfinite(score) and score >= 0
    }
    if not candidates:
        raise AnalysisError("no tempo candidates")
    scored = sorted(
        ((score + _preferred_bonus(candidate, preferred_range), candidate) for candidate, score in candidates.items()),
        reverse=True,
    )
    primary_score, primary = scored[0]
    related = [
        item for item in scored[1:]
        if abs(item[1] - primary * 2) <= 2 or abs(item[1] * 2 - primary) <= 2
    ]
    related.sort(key=lambda item: (item[1] < primary, -item[0]))
    alternate = (related[0] if related else (scored[1] if len(scored) > 1 else (None, None)))[1]
    margin = (
        1.0
        if len(scored) == 1 or primary_score <= 0
        else max(0.0, min(1.0, (primary_score - scored[1][0]) / primary_score))
    )
    ambiguous = len(scored) > 1 and margin < ambiguity_margin
    return TempoDecision(primary, alternate, margin, ambiguous)


def choose_tempo(
    raw_bpm: float,
    candidate_scores: dict[int, float],
    *,
    preferred_range: tuple[int, int] | None = None,
) -> tuple[int, int | None, float]:
    """Compatibility wrapper; use ``choose_tempo_detailed`` for ambiguity."""
    decision = choose_tempo_detailed(raw_bpm, candidate_scores, preferred_range=preferred_range)
    return decision.bpm, decision.alternate_bpm, decision.candidate_margin


def _correlation_strength(onset: object, lag: int) -> float:
    import numpy as np

    values = np.asarray(onset, dtype=float)
    if lag <= 0 or lag >= len(values):
        return 0.0
    left = values[:-lag]
    right = values[lag:]
    denominator = math.sqrt(float(np.dot(left, left) * np.dot(right, right)))
    return max(0.0, float(np.dot(left, right) / denominator)) if denominator else 0.0


def _window_starts(duration: float, count: int, window_duration: float, full_analysis: bool) -> list[tuple[float, float]]:
    if full_analysis:
        return [(0.0, duration)]
    if duration <= window_duration:
        return [(0.0, duration)]
    count = max(1, min(count, 5))
    if count == 1:
        return [(0.0, min(duration, window_duration))]
    starts = [0.0, max(0.0, (duration - window_duration) / 2.0), max(0.0, duration - window_duration)]
    if count > 3:
        starts = [duration * index / count for index in range(count)]
    unique: list[tuple[float, float]] = []
    for start in starts[:count]:
        window = (start, min(window_duration, duration - start))
        if window[1] >= 2.0 and window not in unique:
            unique.append(window)
    return unique or [(0.0, duration)]


def analyze_audio(
    path: Path,
    *,
    min_bpm: int = DEFAULT_MIN_BPM,
    max_bpm: int = DEFAULT_MAX_BPM,
    preferred_range: tuple[int, int] | None = None,
    analysis_windows: int = DEFAULT_ANALYSIS_WINDOWS,
    window_duration: float = DEFAULT_WINDOW_DURATION,
    full_analysis: bool = False,
    ambiguity_margin: float = 0.08,
) -> AnalysisResult:
    import librosa
    import numpy as np

    if min_bpm < 1 or max_bpm < min_bpm:
        raise AnalysisError("rango BPM inválido")
    probe = probe_audio(path)
    if probe.duration < 2.0:
        raise AnalysisError("pista demasiado corta para estimar BPM")
    if full_analysis and probe.duration > MAX_FULL_ANALYSIS_SECONDS:
        raise AnalysisError(
            f"full analysis limitado a {MAX_FULL_ANALYSIS_SECONDS // 60} minutos para proteger memoria; use ventanas"
        )
    windows = _window_starts(probe.duration, analysis_windows, window_duration, full_analysis)
    all_scores: dict[int, list[float]] = {}
    raw_tempos: list[float] = []
    all_intervals: list[float] = []
    total_beats = 0
    analyzed_seconds = 0.0
    analyzed_window_count = 0
    for start, duration in windows:
        pcm = decode_pcm_window(path, start=start, duration=duration, sample_rate=ANALYSIS_SAMPLE_RATE)
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) < ANALYSIS_SAMPLE_RATE * 2:
            continue
        actual_duration = len(samples) / ANALYSIS_SAMPLE_RATE
        analyzed_seconds += min(actual_duration, duration)
        analyzed_window_count += 1
        onset = librosa.onset.onset_strength(
            y=samples, sr=ANALYSIS_SAMPLE_RATE, hop_length=ANALYSIS_HOP_LENGTH
        )
        if not np.any(onset > 0):
            continue
        raw_tempo, beat_frames = librosa.beat.beat_track(
            onset_envelope=onset,
            sr=ANALYSIS_SAMPLE_RATE,
            hop_length=ANALYSIS_HOP_LENGTH,
            trim=False,
        )
        raw = float(np.asarray(raw_tempo).reshape(-1)[0])
        if not math.isfinite(raw) or raw <= 0:
            continue
        raw_tempos.append(raw)
        beat_times = librosa.frames_to_time(
            np.asarray(beat_frames), sr=ANALYSIS_SAMPLE_RATE, hop_length=ANALYSIS_HOP_LENGTH
        )
        total_beats += len(beat_times)
        if len(beat_times) > 2:
            all_intervals.extend(float(value) for value in np.diff(beat_times))
        candidates = tempo_candidates(raw, min_bpm, max_bpm)
        if not candidates:
            continue
        # Autocorrelation and beat-grid agreement avoid choosing solely by librosa's prior.
        for candidate in candidates:
            lag = round(60 * ANALYSIS_SAMPLE_RATE / (ANALYSIS_HOP_LENGTH * candidate))
            correlation = _correlation_strength(onset, lag)
            expected = 60.0 / candidate
            beat_support = 0.0
            if len(beat_times) > 2:
                median_interval = float(np.median(np.diff(beat_times)))
                distance = abs(math.log(max(median_interval, 1e-6) / expected))
                beat_support = max(0.0, 1.0 - min(1.0, distance / 0.35))
            all_scores.setdefault(candidate, []).append(0.7 * correlation + 0.3 * beat_support)
    if not raw_tempos or not all_scores:
        raise AnalysisError("no hay suficiente energía rítmica para estimar BPM")
    raw = float(np.median(raw_tempos))
    canonical_candidates = tempo_candidates(raw, min_bpm, max_bpm)
    normalized_scores: dict[int, list[float]] = {candidate: [] for candidate in canonical_candidates}
    for observed_candidate, scores in all_scores.items():
        if not canonical_candidates:
            continue
        target = min(canonical_candidates, key=lambda candidate: abs(candidate - observed_candidate))
        if abs(target - observed_candidate) <= 4:
            normalized_scores[target].extend(scores)
    aggregate_scores = {
        candidate: float(np.mean(scores))
        for candidate, scores in normalized_scores.items()
        if scores
    }
    decision = choose_tempo_detailed(
        raw,
        aggregate_scores,
        preferred_range=preferred_range,
        ambiguity_margin=ambiguity_margin,
    )
    stability = 0.0
    if all_intervals:
        mean_interval = float(np.mean(all_intervals))
        variation = float(np.std(all_intervals) / mean_interval) if mean_interval else 1.0
        stability = max(0.0, min(1.0, 1.0 - variation))
    coverage = max(0.0, min(1.0, analyzed_seconds / probe.duration))
    quality = max(0.0, min(1.0, 0.45 * stability + 0.4 * decision.candidate_margin + 0.15 * coverage))
    return AnalysisResult(
        duration=probe.duration,
        format_name=probe.format_name,
        bpm=decision.bpm,
        alternate_bpm=decision.alternate_bpm,
        quality_score=round(quality, 4),
        raw_bpm=round(raw, 4),
        stability=round(stability, 4),
        candidate_margin=round(decision.candidate_margin, 4),
        beat_count=total_beats,
        windows_analyzed=analyzed_window_count,
        analysis_coverage=round(coverage, 4),
        ambiguous=decision.ambiguous,
    )
