from __future__ import annotations

import logging
import signal
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Iterable

from .analysis import analyze_audio
from .audio import ProbeInfo, probe_audio
from .database import Database, utc_now
from .discovery import fingerprint_file, is_confined, same_file_state, stat_fingerprint
from .metadata import read_metadata, snapshot_metadata, verify_metadata, write_bpm
from .models import AnalysisResult, FileFingerprint, ProcessingStatus

LOGGER = logging.getLogger(__name__)


@dataclass
class Counters:
    found: int = 0
    skipped_sqlite: int = 0
    skipped_existing_bpm: int = 0
    skipped_failed: int = 0
    ambiguous: int = 0
    processed_ok: int = 0
    written: int = 0
    verified: int = 0
    modified: int = 0
    failed: int = 0


@dataclass(frozen=True)
class ProcessingConfig:
    algorithm_signature: str
    min_bpm: int
    max_bpm: int
    preferred_range: tuple[int, int] | None
    analysis_windows: int
    window_duration: float
    full_analysis: bool
    minimum_quality: float
    ambiguity_margin: float = 0.08


def _save_terminal_failure(
    database: Database,
    fingerprint: FileFingerprint,
    status: ProcessingStatus,
    error: BaseException,
    *,
    config: ProcessingConfig,
) -> None:
    database.save_track(
        fingerprint,
        status=status.value,
        error_message=f"{type(error).__name__}: {error}",
        algorithm_signature=config.algorithm_signature,
        analyzed_at=utc_now(),
    )


def _save_result(
    database: Database,
    fingerprint: FileFingerprint,
    result: AnalysisResult,
    *,
    status: ProcessingStatus,
    config: ProcessingConfig,
    existing_bpm: float | None = None,
    tag_written_at: str | None = None,
    verified_at: str | None = None,
    error_message: str | None = None,
) -> None:
    database.save_track(
        fingerprint,
        format=result.format_name,
        duration=result.duration,
        bpm=result.bpm,
        alternate_bpm=result.alternate_bpm,
        quality_score=result.quality_score,
        raw_bpm=result.raw_bpm,
        stability=result.stability,
        candidate_margin=result.candidate_margin,
        beat_count=result.beat_count,
        windows_analyzed=result.windows_analyzed,
        analysis_coverage=result.analysis_coverage,
        status=status.value,
        error_message=error_message,
        existing_bpm=existing_bpm,
        algorithm_signature=config.algorithm_signature,
        analyzed_at=utc_now(),
        tag_written_at=tag_written_at,
        verified_at=verified_at,
    )


def _probe_unchanged(before: ProbeInfo, after: ProbeInfo) -> None:
    if (before.codec, before.sample_rate, before.channels) != (
        after.codec,
        after.sample_rate,
        after.channels,
    ):
        raise ValueError("verificación de stream fallida: codec, sample rate o canales cambiaron")
    if abs(before.duration - after.duration) > 0.05:
        raise ValueError(
            f"verificación de stream fallida: duración cambió {before.duration:.3f}->{after.duration:.3f}s"
        )


def _analyze(path: Path, config: ProcessingConfig) -> AnalysisResult:
    return analyze_audio(
        path,
        min_bpm=config.min_bpm,
        max_bpm=config.max_bpm,
        preferred_range=config.preferred_range,
        analysis_windows=config.analysis_windows,
        window_duration=config.window_duration,
        full_analysis=config.full_analysis,
        ambiguity_margin=config.ambiguity_margin,
    )


def process_paths(
    paths: Iterable[Path],
    database: Database,
    *,
    workers: int,
    force: bool,
    overwrite_existing_bpm: bool,
    retry_failed: bool,
    config: ProcessingConfig | None = None,
    authorized_root: Path | None = None,
    verify_hashes: bool = False,
    queue_size: int | None = None,
) -> tuple[Counters, bool]:
    if workers < 1:
        raise ValueError("workers debe ser >= 1")
    if queue_size is None or queue_size < 1:
        queue_size = workers * 2
    if config is None:
        from .constants import algorithm_signature

        config = ProcessingConfig(algorithm_signature(), 40, 240, None, 3, 45.0, False, 0.0)
    authorized_root = authorized_root.resolve() if authorized_root is not None else None
    counters = Counters()
    pending: dict[Future[AnalysisResult], tuple[FileFingerprint, float | None]] = {}
    stop_event = Event()
    previous_handler = signal.getsignal(signal.SIGINT)
    started = time.monotonic()

    def stop_handler(_signum: int, _frame: object) -> None:
        stop_event.set()
        LOGGER.warning("signal=SIGINT action=finish-current-and-stop queued_jobs=cancelled")

    def log_progress() -> None:
        elapsed = time.monotonic() - started
        LOGGER.info(
            "event=progress found=%d skipped_sqlite=%d skipped_existing_bpm=%d skipped_failed=%d "
            "ambiguous=%d processed_ok=%d written=%d verified=%d modified=%d failed=%d elapsed=%.2fs speed=%.2f files/s",
            counters.found,
            counters.skipped_sqlite,
            counters.skipped_existing_bpm,
            counters.skipped_failed,
            counters.ambiguous,
            counters.processed_ok,
            counters.written,
            counters.verified,
            counters.modified,
            counters.failed,
            elapsed,
            counters.processed_ok / elapsed if elapsed else 0.0,
        )

    def prepare(path: Path) -> tuple[FileFingerprint, float | None] | None:
        counters.found += 1
        quick = stat_fingerprint(path)
        if verify_hashes:
            candidate = fingerprint_file(path)
            should_process, reason = database.should_process(
                candidate,
                algorithm_signature=config.algorithm_signature,
                force=force,
                retry_failed=retry_failed,
                verify_hashes=True,
            )
        else:
            should_process, reason = database.should_process(
                quick,
                algorithm_signature=config.algorithm_signature,
                force=force,
                retry_failed=retry_failed,
            )
            candidate = quick if not should_process else fingerprint_file(path)
            if should_process:
                should_process, reason = database.should_process(
                    candidate,
                    algorithm_signature=config.algorithm_signature,
                    force=force,
                    retry_failed=retry_failed,
                    verify_hashes=True,
                )
        if not should_process:
            if reason == "sqlite":
                counters.skipped_sqlite += 1
            else:
                counters.skipped_failed += 1
            LOGGER.info("event=skip path=%s reason=%s", path, reason)
            log_progress()
            return None
        metadata = read_metadata(path)
        if metadata.existing_bpm is not None and not overwrite_existing_bpm:
            database.save_track(
                candidate,
                format=metadata.format_name,
                bpm=metadata.existing_bpm,
                existing_bpm=metadata.existing_bpm,
                bpm_source="existing",
                status=ProcessingStatus.SKIPPED_EXISTING_BPM.value,
                error_message=None,
                algorithm_signature=config.algorithm_signature,
                analyzed_at=utc_now(),
            )
            counters.skipped_existing_bpm += 1
            LOGGER.info("event=skip path=%s reason=existing-bpm bpm=%s", path, metadata.existing_bpm)
            log_progress()
            return None
        return candidate, metadata.existing_bpm

    signal.signal(signal.SIGINT, stop_handler)
    iterator = iter(paths)
    exhausted = False
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bpm") as executor:
            while pending or not exhausted:
                while not stop_event.is_set() and not exhausted and len(pending) < queue_size:
                    try:
                        path = next(iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    try:
                        prepared = prepare(path)
                        if prepared is not None:
                            fingerprint, existing_bpm = prepared
                            pending[executor.submit(_analyze, fingerprint.path, config)] = (
                                fingerprint,
                                existing_bpm,
                            )
                    except Exception as error:
                        counters.failed += 1
                        LOGGER.error("event=failed path=%s stage=prepare error=%s", path, error, exc_info=LOGGER.isEnabledFor(logging.DEBUG))
                        try:
                            _save_terminal_failure(
                                database,
                                fingerprint_file(path),
                                ProcessingStatus.FAILED,
                                error,
                                config=config,
                            )
                        except (OSError, RuntimeError) as persist_error:
                            LOGGER.error("event=failed path=%s stage=persist-error error=%s", path, persist_error)
                        log_progress()
                if not pending:
                    continue
                done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    fingerprint, existing_bpm = pending.pop(future)
                    terminal_recorded = False
                    try:
                        result = future.result()
                        if result.ambiguous or result.quality_score < config.minimum_quality:
                            reason = "ambiguous" if result.ambiguous else "quality-below-threshold"
                            _save_result(
                                database,
                                fingerprint,
                                result,
                                status=ProcessingStatus.AMBIGUOUS,
                                config=config,
                                error_message=reason,
                            )
                            counters.ambiguous += 1
                            LOGGER.warning("event=ambiguous path=%s bpm=%s alternate=%s quality=%.3f reason=%s", fingerprint.path, result.bpm, result.alternate_bpm, result.quality_score, reason)
                            log_progress()
                            continue
                        if authorized_root is not None and not is_confined(fingerprint.path, authorized_root):
                            raise PermissionError(f"ruta fuera del root autorizado: {fingerprint.path}")
                        # Hash/stat check is deliberately immediately before metadata I/O.
                        current = fingerprint_file(fingerprint.path)
                        if not same_file_state(fingerprint, current):
                            _save_terminal_failure(
                                database,
                                current,
                                ProcessingStatus.CHANGED_DURING_ANALYSIS,
                                RuntimeError("el archivo cambió durante el análisis"),
                                config=config,
                            )
                            counters.failed += 1
                            LOGGER.error("event=changed-during-analysis path=%s", fingerprint.path)
                            log_progress()
                            continue
                        before_tags = snapshot_metadata(fingerprint.path)
                        before_probe = probe_audio(fingerprint.path)
                        _save_result(database, fingerprint, result, status=ProcessingStatus.ANALYZED, config=config, existing_bpm=existing_bpm)
                        try:
                            modified = write_bpm(fingerprint.path, result.bpm)
                        except Exception as error:
                            _save_terminal_failure(database, fingerprint, ProcessingStatus.WRITE_FAILED, error, config=config)
                            terminal_recorded = True
                            raise
                        written_at = utc_now() if modified else None
                        _save_result(database, fingerprint, result, status=ProcessingStatus.WRITTEN, config=config, existing_bpm=existing_bpm, tag_written_at=written_at)
                        try:
                            verify_metadata(fingerprint.path, result.bpm, before_tags)
                            _probe_unchanged(before_probe, probe_audio(fingerprint.path))
                        except Exception as error:
                            _save_terminal_failure(database, fingerprint, ProcessingStatus.VERIFICATION_FAILED, error, config=config)
                            terminal_recorded = True
                            raise
                        final_fingerprint = fingerprint_file(fingerprint.path)
                        _save_result(database, final_fingerprint, result, status=ProcessingStatus.VERIFIED, config=config, existing_bpm=existing_bpm, tag_written_at=written_at, verified_at=utc_now())
                        counters.processed_ok += 1
                        counters.written += 1
                        counters.verified += 1
                        counters.modified += int(modified)
                        LOGGER.info("event=verified path=%s bpm=%s alternate=%s quality=%.3f modified=%s", fingerprint.path, result.bpm, result.alternate_bpm, result.quality_score, modified)
                        log_progress()
                    except Exception as error:
                        counters.failed += 1
                        if not terminal_recorded:
                            try:
                                _save_terminal_failure(
                                    database,
                                    fingerprint,
                                    ProcessingStatus.FAILED,
                                    error,
                                    config=config,
                                )
                            except (OSError, RuntimeError) as persist_error:
                                LOGGER.error("event=failed path=%s stage=persist-error error=%s", fingerprint.path, persist_error)
                        LOGGER.error("event=failed path=%s stage=analyze-write-verify error=%s", fingerprint.path, error, exc_info=LOGGER.isEnabledFor(logging.DEBUG))
                        log_progress()
                if stop_event.is_set():
                    for future in pending:
                        future.cancel()
                    break
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    elapsed = time.monotonic() - started
    LOGGER.info("event=summary found=%d skipped_sqlite=%d skipped_existing_bpm=%d skipped_failed=%d ambiguous=%d processed_ok=%d written=%d verified=%d modified=%d failed=%d elapsed=%.2fs speed=%.2f files/s", counters.found, counters.skipped_sqlite, counters.skipped_existing_bpm, counters.skipped_failed, counters.ambiguous, counters.processed_ok, counters.written, counters.verified, counters.modified, counters.failed, elapsed, counters.processed_ok / elapsed if elapsed else 0.0)
    return counters, stop_event.is_set()
