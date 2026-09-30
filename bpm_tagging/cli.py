from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .audio import decode_pcm_window, probe_audio
from .constants import (
    DEFAULT_ANALYSIS_WINDOWS,
    DEFAULT_DATABASE,
    DEFAULT_MAX_BPM,
    DEFAULT_MIN_BPM,
    DEFAULT_QUEUE_SIZE,
    DEFAULT_WINDOW_DURATION,
    SUPPORTED_EXTENSIONS,
    algorithm_signature,
    validate_tools,
)
from .database import Database
from .discovery import is_confined, iter_discovery, normalize_extensions, stat_fingerprint
from .metadata import can_write_metadata, read_metadata
from .processor import ProcessingConfig, process_paths

LOGGER = logging.getLogger(__name__)


def _range(value: str) -> tuple[int, int]:
    try:
        low_text, high_text = value.replace(":", "-").split("-", 1)
        low, high = int(low_text), int(high_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("debe tener formato MIN-MAX") from exc
    if low < 1 or high < low:
        raise argparse.ArgumentTypeError("rango BPM inválido")
    return low, high


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect and write BPM tags recursively.")
    parser.add_argument("root", type=Path, help="directory to scan recursively")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--force", action="store_true", help="reanalyze successful and skipped files")
    parser.add_argument("--overwrite-existing-bpm", action="store_true")
    parser.add_argument("--workers", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--queue-size", type=int, default=DEFAULT_QUEUE_SIZE, help="bounded analysis queue (default: workers*2)")
    parser.add_argument("--extensions", nargs="+", help="extensions, optionally comma-separated (default: all supported)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true", help="preflight without writing tags or SQLite")
    parser.add_argument("--verify-hashes", action="store_true", help="hash unchanged files before skipping")
    parser.add_argument("--allow-symlinks", action="store_true", help="allow links only when their resolved target stays inside root")
    parser.add_argument("--min-bpm", type=int, default=DEFAULT_MIN_BPM)
    parser.add_argument("--max-bpm", type=int, default=DEFAULT_MAX_BPM)
    parser.add_argument("--preferred-bpm-range", type=_range)
    parser.add_argument("--profile", choices=("eurobeat",))
    parser.add_argument("--minimum-quality", type=float, default=0.0)
    parser.add_argument("--analysis-windows", type=int, default=DEFAULT_ANALYSIS_WINDOWS)
    parser.add_argument("--window-duration", type=float, default=DEFAULT_WINDOW_DURATION)
    parser.add_argument("--full-analysis", action="store_true")
    parser.add_argument("--show-config", action="store_true", help="show effective configuration and exit")
    return parser


def configure_logging(verbose: bool) -> None:
    logging.getLogger("numba").setLevel(logging.WARNING)
    logging.getLogger("librosa").setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _effective_config(args: argparse.Namespace, versions: dict[str, str]) -> ProcessingConfig:
    preferred = args.preferred_bpm_range
    if args.profile == "eurobeat":
        if preferred is not None:
            raise ValueError("--profile eurobeat y --preferred-bpm-range son mutuamente excluyentes")
        preferred = (120, 190)
    if not 1 <= args.min_bpm <= args.max_bpm:
        raise ValueError("--min-bpm/--max-bpm inválidos")
    if not 0.0 <= args.minimum_quality <= 1.0:
        raise ValueError("--minimum-quality debe estar entre 0 y 1")
    if args.analysis_windows < 1 or args.analysis_windows > 5:
        raise ValueError("--analysis-windows debe estar entre 1 y 5")
    if args.window_duration <= 0:
        raise ValueError("--window-duration debe ser > 0")
    signature = algorithm_signature(
        min_bpm=args.min_bpm,
        max_bpm=args.max_bpm,
        preferred_range=preferred,
        analysis_windows=args.analysis_windows,
        window_duration=args.window_duration,
        full_analysis=args.full_analysis,
        librosa_version=versions["librosa"],
        ffmpeg_version=versions["ffmpeg"],
        ffprobe_version=versions["ffprobe"],
    )
    return ProcessingConfig(signature, args.min_bpm, args.max_bpm, preferred, args.analysis_windows, args.window_duration, args.full_analysis, args.minimum_quality)


def _dry_run(
    paths: list[Path],
    root: Path,
    database_path: Path | None,
    config: ProcessingConfig,
    verify_hashes: bool,
    *,
    force: bool,
    retry_failed: bool,
    overwrite_existing_bpm: bool,
) -> int:
    failures = 0
    database: Database | None = None
    if database_path is not None and database_path.exists():
        try:
            database = Database(database_path, read_only=True)
        except (OSError, RuntimeError) as exc:
            LOGGER.error("event=dry-run database=error error=%s", exc)
            return 1
    try:
        for path in paths:
            try:
                if not is_confined(path, root):
                    raise PermissionError("ruta fuera del root autorizado")
                fingerprint = stat_fingerprint(path)
                if not os.access(path, os.R_OK):
                    raise PermissionError("sin permiso de lectura")
                metadata = read_metadata(path)
                can_write_metadata(path)
                probe = probe_audio(path)
                decode_pcm_window(path, start=0.0, duration=min(0.5, max(0.1, probe.duration)), sample_rate=22050)
                reason = "new-or-changed"
                if database is not None:
                    if verify_hashes:
                        from .discovery import fingerprint_file

                        fingerprint = fingerprint_file(path)
                    should, reason = database.should_process(
                        fingerprint,
                        algorithm_signature=config.algorithm_signature,
                        force=force,
                        retry_failed=retry_failed,
                        verify_hashes=verify_hashes,
                    )
                    action = "process" if should else "skip"
                else:
                    should, reason = True, "new-or-changed"
                    action = "process"
                if metadata.existing_bpm is not None and not overwrite_existing_bpm:
                    action = "skip-existing-bpm"
                LOGGER.info("event=preflight path=%s action=%s reason=%s format=%s duration=%.2fs existing_bpm=%s", path, action, reason, metadata.format_name, probe.duration, metadata.existing_bpm)
            except Exception as exc:
                failures += 1
                LOGGER.error("event=preflight-failed path=%s error=%s", path, exc, exc_info=LOGGER.isEnabledFor(logging.DEBUG))
    finally:
        if database is not None:
            database.close()
    return int(failures > 0)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)
    if args.workers < 1 or args.queue_size < 0:
        raise SystemExit("--workers debe ser >= 1 y --queue-size no puede ser negativo")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit debe ser >= 1")
    extensions = normalize_extensions(args.extensions)
    unknown = extensions - SUPPORTED_EXTENSIONS
    if unknown:
        raise SystemExit(f"extensiones no soportadas: {', '.join(sorted(unknown))}")
    try:
        versions = validate_tools()
        config = _effective_config(args, versions)
        root_files, issues, resolved_root = iter_discovery(args.root, extensions, allow_symlinks=args.allow_symlinks)
    except (OSError, RuntimeError, ValueError) as error:
        LOGGER.error("event=initialization-failed error=%s", error)
        return 2
    for issue in issues:
        LOGGER.warning("event=discovery-skip path=%s reason=%s", issue.path, issue.reason)
    paths = root_files[: args.limit] if args.limit is not None else root_files
    database_path = (args.database or DEFAULT_DATABASE).expanduser().resolve()
    if args.show_config:
        LOGGER.info("event=config root=%s database=%s files=%d workers=%d queue_size=%d algorithm_signature=%s min_bpm=%d max_bpm=%d preferred_range=%s windows=%d window_duration=%.1f full_analysis=%s", resolved_root, database_path, len(paths), args.workers, args.queue_size or args.workers * 2, config.algorithm_signature, config.min_bpm, config.max_bpm, config.preferred_range, config.analysis_windows, config.window_duration, config.full_analysis)
        return 0
    LOGGER.info("event=discovery root=%s files=%d skipped=%d extensions=%s", resolved_root, len(paths), len(issues), sorted(extensions))
    if args.dry_run:
        return _dry_run(paths, resolved_root, database_path, config, args.verify_hashes, force=args.force, retry_failed=args.retry_failed, overwrite_existing_bpm=args.overwrite_existing_bpm)
    try:
        with Database(database_path) as database:
            counters, interrupted = process_paths(paths, database, workers=args.workers, force=args.force, overwrite_existing_bpm=args.overwrite_existing_bpm, retry_failed=args.retry_failed, config=config, authorized_root=resolved_root, verify_hashes=args.verify_hashes, queue_size=args.queue_size)
    except KeyboardInterrupt:
        LOGGER.warning("event=interrupted")
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        LOGGER.error("event=execution-failed error=%s", error)
        return 2
    return 130 if interrupted else int(counters.failed > 0)


if __name__ == "__main__":
    sys.exit(main())
