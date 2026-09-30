# bpm-tagging

CLI incremental para estimar BPM con `librosa` y escribir únicamente el tag
BPM mediante `mutagen`. El stream de audio no se recodifica ni se reemplaza.
Los formatos admitidos son `.opus`, `.ogg`, `.flac`, `.mp3`, `.m4a` y `.wav`.
Los recorridos son recursivos y excluyen por extensión archivos como
`checksum.md5`.

[English](#english) · [Español](#español)

## English

An incremental CLI for estimating BPM with `librosa` and writing only the BPM
tag through `mutagen`. Audio streams are not re-encoded or replaced. Supported
formats are `.opus`, `.ogg`, `.flac`, `.mp3`, `.m4a`, and `.wav`. Directory
scans are recursive and ignore non-audio extensions such as `checksum.md5`.

### Installation

Requires Python 3.12+, `ffmpeg`, and `ffprobe` available on `PATH`.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

The `dev` extra installs Ruff and mypy in addition to `librosa` and `mutagen`.
The librosa/mutagen versions have upper bounds in `pyproject.toml`; each run's
signature includes their versions, ffmpeg/ffprobe versions, and all parameters
that affect analysis.

### Safe usage

Run a preflight on a sample first:

```bash
.venv/bin/python -m bpm_tagging /path/to/library \
  --database /path/to/state.sqlite3 --limit 3 --dry-run --verbose
```

Then run the same validated sample:

```bash
.venv/bin/python -m bpm_tagging /path/to/library \
  --database /path/to/state.sqlite3 --limit 3 --workers 2 \
  --overwrite-existing-bpm --verbose
```

The `bpm-tagging` executable is also installed. Important options include:

- `--database PATH`: explicit state database; takes precedence over the default.
- `--force`: reprocesses tracks even when their state is current.
- `--retry-failed`: retries `failed`, `write_failed`, `verification_failed`, and
  `changed_during_analysis`; the retry reason is recorded as `retry_failed`.
- `--overwrite-existing-bpm`: replaces an existing valid BPM. Without it, the
  existing value is preserved and recorded as `skipped_existing_bpm`.
- `--min-bpm`, `--max-bpm`, `--preferred-bpm-range MIN-MAX`, and
  `--profile eurobeat`: explicit policy for half/double-time candidates.
- `--minimum-quality`: prevents writing results below the selected threshold.
- `--analysis-windows`, `--window-duration`, and `--full-analysis`: control
  temporal coverage. By default, up to three representative windows are
  decoded to mono PCM through a pipe instead of creating a full temporary WAV.
- `--verify-hashes`: forces SHA-256 verification before skipping unchanged tracks.
- `--queue-size`: limits pending Futures; defaults to `workers * 2`.
- `--allow-symlinks`: permits only links resolving inside the root; symlinks are
  rejected by default.
- `--show-config`: prints the effective algorithm signature and exits.

The default SQLite location is `$XDG_STATE_HOME/bpm-tagging/state.sqlite3`; if
`XDG_STATE_HOME` is not set, it is `~/.local/state/bpm-tagging/state.sqlite3`.
Using `--database` is recommended when selecting the state location explicitly.

### Path safety and race handling

The root is resolved once. Discovery does not follow symlinks by default,
avoids directory-identity cycles, and records skipped entries. Before writing,
the resolved path is checked again against the root, and size, `mtime_ns`,
`st_dev`, `st_ino`, and SHA-256 are compared with the state used for analysis.
If the file was replaced or changed, BPM is not written and
`changed_during_analysis` is recorded for a later retry.

Files are modified directly and persistent backups are not created. Mutagen's
container-specific behavior is used for writing; there is no universal atomic
replacement for all six formats. Each write is reopened and verified with
Mutagen, and ffprobe validates that codec, duration (maximum documented
tolerance: 50 ms), sample rate, channels, and the audio stream remain valid.
Permissions, ACLs, and xattrs are deliberately not rewritten; direct writes
preserve the inode and avoid replacing the complete file.

### Tempo and quality

Analysis uses an onset envelope, autocorrelation, beat-grid consistency, and
multiple windows. Normal, half-time, and double-time candidates are evaluated
within the configured limits. `raw_bpm` is librosa's initial aggregated output;
`alternate_bpm` is the most relevant related half/double candidate.

`quality_score` is not a calibrated probability. It is a heuristic metric in
`[0, 1]` combining:

- 45% beat-interval stability;
- 40% relative margin between candidates;
- 15% analyzed temporal coverage.

The database also stores `stability`, `candidate_margin`, `beat_count`,
`windows_analyzed`, and `analysis_coverage`. A perfectly regular sequence may
not provide evidence to distinguish 90 from 180 BPM; it is marked `ambiguous`
and is not written automatically. A preferred range is an operator policy, not
a universal musical probability. `eurobeat` uses 120–190 BPM as an explicit
example and is included in the algorithm signature.

Very short or silent tracks fail in a controlled way. Use `--minimum-quality`
when additional manual review is required.

### SQLite and states

The current schema is `SCHEMA_VERSION=2` and migrates v1 databases while
preserving rows. Opening a database newer than the current code fails cleanly.
The `tracks` table includes absolute path, size, `mtime_ns`, hash, file
identity, format, duration, BPM, alternate BPM, raw BPM, quality metrics,
window and coverage data, algorithm/version information, status, errors, and
timestamps.

Relevant terminal states are `verified`, `skipped_existing_bpm`, `ambiguous`,
`failed`, `write_failed`, `verification_failed`, and
`changed_during_analysis`; `analyzed` and `written` may be observed during a
write. Only `verified` avoids later analysis. Each transition writes a complete
row and clears incompatible results, so a failure cannot leave a stale BPM that
appears current.

The fast path checks path, size, `mtime_ns`, file identity, and signature first.
If they match, it skips a full read and SHA-256 calculation. This is faster but
does not detect a modification preserving exactly the same size and `mtime_ns`;
`--verify-hashes` removes that trade-off at the cost of I/O.

### Interrupts, concurrency, and exit status

Analysis uses at most `queue-size` pending Futures, while tag and SQLite writes
are serialized. `Ctrl+C` cancels jobs not yet started, starts no new files,
allows an active analysis/write to finish, and returns exit code 130. Verified
results remain in SQLite.

Exit codes:

- `0`: execution completed without failures;
- `1`: one or more files failed or could not be verified;
- `2`: configuration, tool, database, or discovery error;
- `130`: interrupted with `Ctrl+C`.

The summary distinguishes found tracks, SQLite skips, existing BPM skips,
previous failures, ambiguous results, analyzed tracks, writes, verified tracks,
modifications, elapsed time, and average speed.

### External checksums

Changing metadata changes the complete-file checksum even when the codec and
audio stream do not change. The tool does not update `checksum.md5` or any
external manifest and reports this operational risk. The SQLite SHA-256 is for
the complete database file and must not be confused with an audio-stream hash.

### Navidrome

After tagging, run a **full scan** from Navidrome's interface or administrative
mechanism so it rereads the tags. Navidrome manifests and databases are not
modified automatically.

### Tests and validation

All tests use temporary directories or synthetic audio; they do not process a
real music library. Coverage includes real metadata writes for Opus, Ogg
Vorbis, FLAC, MP3, M4A, and WAV, tag preservation, stream verification, SQLite
migration, fast path, external symlinks, races, errors, dry-run, tempo
selection, and a second incremental run.

```bash
.venv/bin/python -m compileall -q bpm_tagging tests
.venv/bin/python -m unittest discover -v
.venv/bin/ruff check bpm_tagging tests
.venv/bin/mypy bpm_tagging
```

Do not run a complete library scan without repeating preflight and validating a
sample first.

## Español

### Instalación

Requiere Python 3.12+, `ffmpeg` y `ffprobe` accesibles en `PATH`.

```bash
cd /path/to/bpm-tagging
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

`dev` instala Ruff y mypy además de `librosa` y `mutagen`. Las versiones de
librosa/mutagen tienen límites superiores en `pyproject.toml`; la firma de
cada ejecución incluye sus versiones, ffmpeg/ffprobe y todos los parámetros
que afectan al análisis.

## Uso seguro

Primero ejecutar un preflight sobre una muestra:

```bash
.venv/bin/python -m bpm_tagging /ruta/a/la/biblioteca \
  --database /ruta/estado.sqlite3 --limit 3 --dry-run --verbose
```

Después, sobre la misma muestra validada:

```bash
.venv/bin/python -m bpm_tagging /ruta/a/la/biblioteca \
  --database /ruta/estado.sqlite3 --limit 3 --workers 2 \
  --overwrite-existing-bpm --verbose
```

También existe el ejecutable `bpm-tagging`. Opciones importantes:

- `--database PATH`: base explícita; tiene precedencia sobre el valor predeterminado.
- `--force`: reprocesa aunque el estado sea vigente.
- `--retry-failed`: reintenta `failed`, `write_failed`, `verification_failed` y
  `changed_during_analysis`; el motivo queda registrado como `retry_failed`.
- `--overwrite-existing-bpm`: permite reemplazar BPM válido ya presente. Sin
  esta opción se conserva y se registra como `skipped_existing_bpm`.
- `--min-bpm`, `--max-bpm`, `--preferred-bpm-range MIN-MAX` y
  `--profile eurobeat`: política explícita para candidatos half/double-time.
- `--minimum-quality`: no escribe resultados por debajo del umbral.
- `--analysis-windows`, `--window-duration` y `--full-analysis`: controlan la
  cobertura temporal. Por defecto se decodifican hasta tres ventanas
  representativas a PCM mono por pipe, no un WAV temporal completo.
- `--verify-hashes`: fuerza SHA-256 antes de omitir pistas sin cambios.
- `--queue-size`: limita Futures pendientes; por defecto es `workers * 2`.
- `--allow-symlinks`: solo permite enlaces cuyo destino resuelto sigue dentro
  del root; por defecto todos los symlinks se rechazan.
- `--show-config`: muestra la firma efectiva del algoritmo y termina.

La ubicación predeterminada de SQLite es `$XDG_STATE_HOME/bpm-tagging/state.sqlite3`;
si `XDG_STATE_HOME` no existe, `~/.local/state/bpm-tagging/state.sqlite3`. La
opción `--database` siempre es la forma recomendada para seleccionar el estado.

## Seguridad de rutas y carreras

El root se resuelve una sola vez. El descubrimiento no sigue symlinks por
defecto, evita ciclos por identidad de directorio y registra los omitidos.
Antes de escribir se vuelve a comprobar que la ruta resuelta pertenece al root,
y se comparan tamaño, `mtime_ns`, `st_dev`, `st_ino` y SHA-256 con el estado usado
para el análisis. Si el archivo fue reemplazado o cambió, no se escribe BPM y
se registra `changed_during_analysis` para reintento posterior.

Los archivos se modifican directamente y no se crean backups persistentes. La
escritura depende del comportamiento de `mutagen` de cada contenedor; no hay un
reemplazo atómico universal para estos seis formatos. Por eso cada escritura
se reabre y verifica con Mutagen, y se valida con ffprobe que codec, duración
(tolerancia máxima documentada: 50 ms), sample rate, canales y stream de audio
permanezcan válidos. Permisos, ACLs y xattrs no se reescriben deliberadamente;
una escritura directa conserva el inode y evita la semántica más peligrosa de
reemplazar el archivo completo.

## Tempo y calidad

El análisis usa onset envelope, autocorrelación, consistencia de la rejilla de
beats y varias ventanas. Evalúa candidatos normal, mitad y doble dentro de los
límites configurados. `raw_bpm` es la salida inicial agregada de librosa;
`alternate_bpm` es el candidato relacionado half/double más relevante.

`quality_score` no es una probabilidad calibrada. Es una métrica heurística en
`[0, 1]` que combina:

- 45% de estabilidad de intervalos de beat;
- 40% de margen relativo entre candidatos;
- 15% de cobertura temporal analizada.

La base guarda también `stability`, `candidate_margin`, `beat_count`,
`windows_analyzed` y `analysis_coverage`. Una secuencia perfectamente regular
puede no aportar evidencia para distinguir 90 de 180 BPM; se marca
`ambiguous` y no se escribe automáticamente. Un rango preferido es una
política del operador, no una probabilidad musical universal. `eurobeat` usa
120–190 BPM como ejemplo explícito y queda incluido en la firma del algoritmo.

Pistas demasiado cortas o silenciosas fallan de forma controlada. Se puede
usar `--minimum-quality` para exigir revisión manual adicional.

## SQLite y estados

El esquema actual es `SCHEMA_VERSION=2` y migra bases v1 conservando filas. Al
abrir una base más nueva que el código, la herramienta falla limpiamente. La
tabla `tracks` incluye ruta absoluta, tamaño, `mtime_ns`, hash, identidad del
archivo, formato, duración, BPM, alternativo, raw BPM, métricas de calidad,
ventanas, cobertura, firma/versiones del algoritmo, estado, error y fechas.

Los estados terminales relevantes son `verified`, `skipped_existing_bpm`,
`ambiguous`, `failed`, `write_failed`, `verification_failed` y
`changed_during_analysis`; durante una escritura pueden observarse
`analyzed` y `written`. Solo `verified` evita un análisis posterior. Cada
transición escribe una fila completa y limpia resultados incompatibles, por lo
que un fallo no conserva un BPM que parezca vigente.

El fast path consulta primero ruta, tamaño, `mtime_ns`, identidad y firma. Si
coinciden, omite sin leer el archivo completo ni calcular SHA-256. Esto es más
rápido, pero no detecta una modificación que conserve exactamente tamaño y
`mtime_ns`; `--verify-hashes` elimina ese trade-off a costa de I/O.

## Interrupciones, concurrencia y salida

El análisis usa como máximo `queue-size` Futures y la escritura de tags/SQLite
es serializada. `Ctrl+C` cancela trabajos aún no iniciados, no inicia nuevos
archivos, permite terminar un análisis/escritura ya iniciado y devuelve exit
code 130. Los resultados ya verificados permanecen en SQLite.

Exit codes:

- `0`: ejecución sin fallos;
- `1`: uno o más archivos fallaron o no pudieron verificarse;
- `2`: error de configuración, herramientas, base o descubrimiento;
- `130`: interrupción por `Ctrl+C`.

El resumen distingue encontrados, omisiones por SQLite, BPM existente,
fallidos previos, ambiguos, analizados, escritos, verificados, modificados,
tiempo transcurrido y velocidad media.

## Checksums externos

Modificar metadata cambia el checksum del archivo completo aunque el codec y el
stream no cambien. La herramienta no actualiza `checksum.md5` ni ningún
manifest externo y lo reporta como riesgo operativo. El SHA-256 de SQLite es
del archivo completo; no debe confundirse con una huella del stream de audio.

## Navidrome

Después de etiquetar, ejecutar un **full scan** desde la interfaz o el
mecanismo administrativo de Navidrome para que relea los tags. No se modifican
automáticamente manifests ni la base de datos de Navidrome.

## Pruebas y validación

Todas las pruebas usan temporales o audio sintético; no procesan la biblioteca
real. La matriz cubre escritura real en Opus, Ogg Vorbis, FLAC, MP3, M4A y WAV,
preservación de tags, verificación de stream, migración SQLite, fast path,
symlinks externos, carreras, errores, dry-run, selección de tempo y una
segunda ejecución incremental.

```bash
.venv/bin/python -m compileall -q bpm_tagging tests
.venv/bin/python -m unittest discover -v
.venv/bin/ruff check bpm_tagging tests
.venv/bin/mypy bpm_tagging
```

No ejecutar el lote completo sin repetir el preflight y validar una muestra.
