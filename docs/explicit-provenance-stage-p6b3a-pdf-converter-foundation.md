# Explicit Provenance Stage P6-B3a: PDF Converter Foundation

## Purpose

Stage P6-B3a gives Explicit Provenance a real, addressable DOCX->PDF
converter foundation: exact DOCX bytes go in, exact PDF bytes (plus their
SHA-256) come out, or a closed failure status comes out -- never a raw
exception, never a silently truncated buffer, never an orphaned process or
temp directory.

Production support is **macOS and Linux only**. On Windows the converter
always fail-closed returns `CONVERTER_UNAVAILABLE` without ever attempting
a subprocess, a `Job Object`, or any other Windows-specific process
control -- that is explicitly out of scope for this stage.

## Scope

P6-B3a implements:

1. `ConverterRuntimeIdentity` -- a closed, content-derived description of
   *exactly which* `soffice` binary was observed, with a fail-closed
   verified `runtime_identity_fingerprint`;
2. `DocxToPdfConversionResult` / `DocxToPdfConversionStatus` -- a closed
   result contract whose success/failure invariants are enforced by the
   model itself;
3. `DocxToPdfConverter` -- a new, `runtime_checkable` Protocol, distinct
   from and never a replacement for the existing P6-A
   `CvPdfConversionAdapter` (`app/services/cv_document_adapters.py`),
   which this stage never modifies;
4. `PosixProcessRunner` -- bounded subprocess execution with bounded
   stdout/stderr, a single central coordinator, and process-group
   timeout/overflow cleanup;
5. executable identity observation and secure PDF output read
   (`cv_document_secure_pdf_reader.py`);
6. `LibreOfficeDocxToPdfConverter` -- the real adapter tying all of the
   above together, including isolated per-call workspaces and a cleanup
   security contract.

It implements **no** `FinalDocxSnapshot` repository wiring, no Confirmed
PDF artifact, no SQL tables, no migration, no current-PDF slot, no replay
persistence, no reconciliation, no API, no router, no frontend, and no
"Generate PDF" button.

## Runtime identity

`ConverterRuntimeIdentity` (`app/schemas/cv_document_pdf_conversion_runtime.py`)
is `frozen=True`/`extra="forbid"` and carries:

- `implementation_id`, `implementation_version`,
- `executable_canonical_path`, `executable_file_identity`,
- `executable_sha256` (mandatory -- there is no "optional hash" mode),
- `platform_identity`, `font_environment_id`,
- `runtime_identity_schema_version`,
- `runtime_identity_fingerprint`.

`runtime_identity_fingerprint` is always recomputed by the model's own
`model_validator` from every other field plus the explicit schema version
(`compute_converter_runtime_identity_fingerprint`) and the model
fail-closed rejects any mismatch -- a caller can never hand in an
unverified fingerprint and have it accepted at face value.
`build_converter_runtime_identity` is the intended construction path.

## Conversion result contract

`DocxToPdfConversionResult` (`app/schemas/cv_document_pdf_conversion.py`)
enforces its own invariants rather than relying on producer discipline:

- `SUCCEEDED` requires non-empty `pdf_bytes`, a `pdf_sha256` that exactly
  matches `sha256(pdf_bytes)`, a `runtime_identity`, and no `error_code`;
- every other `DocxToPdfConversionStatus` requires no `pdf_bytes`/
  `pdf_sha256` and a mandatory, non-empty `error_code`.

`diagnostics` is modelled as a bounded tuple (at most 8 entries, each at
most 1000 characters, no NUL bytes, defensively copied) -- content never
leaks caller document bytes.

Closed statuses: `SUCCEEDED`, `INVALID_REQUEST`, `CONVERTER_UNAVAILABLE`,
`PROCESS_START_FAILED`, `PROCESS_OUTPUT_LIMIT_EXCEEDED`,
`CONVERSION_TIMEOUT`, `PROCESS_TERMINATION_FAILED`, `CONVERSION_FAILED`,
`PDF_OUTPUT_MISSING`, `PDF_OUTPUT_LOCKED`, `PDF_OUTPUT_UNREADABLE`,
`PDF_OUTPUT_CHANGED_DURING_READ`, `PDF_OUTPUT_TOO_LARGE`,
`PDF_OUTPUT_INVALID`, `WORKSPACE_SETUP_FAILED`,
`WORKSPACE_CLEANUP_FAILED`, `CONVERTER_CONTRACT_VIOLATION`.

## The `DocxToPdfConverter` Protocol

`app/services/cv_document_pdf_converter_protocol.py` defines a new,
`runtime_checkable` Protocol:

```python
class DocxToPdfConverter(Protocol):
    @property
    def runtime_identity(self) -> ConverterRuntimeIdentity | None: ...

    def convert_docx_to_pdf(
        self, *, docx_bytes: bytes, source_snapshot_fingerprint: str,
        source_docx_sha256: str, conversion_policy_version: str,
    ) -> DocxToPdfConversionResult: ...
```

It never accepts a filesystem path, filename, executable, argv, env,
output path, or expected PDF hash from a caller. It is a distinct contract
from the existing P6-A `CvPdfConversionAdapter`
(`app/services/cv_document_adapters.py`), which this stage **never
modifies** (verified by a dedicated adapter test).

## Request validation (fail-closed, pre-workspace)

Before a workspace is ever created, `LibreOfficeDocxToPdfConverter`
validates: `docx_bytes` is non-empty and within `max_docx_size_bytes`,
both `source_snapshot_fingerprint` and `source_docx_sha256` are exactly 64
lowercase hex characters, `sha256(docx_bytes) == source_docx_sha256`, and
`conversion_policy_version` is non-empty and bounded. Any violation
returns `INVALID_REQUEST` with diagnostics that never echo document
content -- and never creates a workspace, writes a DOCX, or starts a
process.

## Executable identity: mandatory SHA-256 and per-request revalidation

`observe_executable_identity` (`app/services/cv_document_secure_pdf_reader.py`)
is the shared, POSIX-only helper both the constructor and every
conversion call use: absolute-path check -> `lstat` -> reject symlink ->
reject non-regular (before ever opening, so a FIFO can never block this
call) -> `open(O_RDONLY | O_NOFOLLOW)` -> `fstat` -> path/fd identity match
-> bounded chunked SHA-256 to EOF -> `fstat` after -> final `lstat` ->
final path/fd identity match -> close exactly once.

At construction, the adapter observes the executable once, runs a bounded
`soffice --version`, and caches the resulting `ConverterRuntimeIdentity`
for the adapter's lifetime. **Before every conversion call**, it re-runs
`observe_executable_identity` and compares canonical path, file identity,
and `executable_sha256` against the cached identity -- any mismatch
returns `CONVERTER_UNAVAILABLE` without ever starting a conversion
process. The cache is never trusted without this per-request
revalidation.

### Residual executable verification-to-exec race (not eliminated)

`observe_executable_identity` always closes its file descriptor before
returning -- it never keeps a verified handle open across the subsequent
`Popen` call. Per-request revalidation therefore **minimizes, but does not
eliminate**, the window between "we just verified this exact executable's
SHA-256 at this exact inode" and "the operating system independently
reopens and executes whatever is on disk at `soffice_path` when
`ProcessRunner.run` calls `Popen`." The file the OS actually executes is
re-opened by the kernel at `Popen` time, entirely independently of the
handle used during revalidation; this stage makes **no claim** that the
verified file descriptor is cryptographically or otherwise bound to the
process the OS ultimately runs.

This stage deliberately does not attempt an `fexecve`/`/proc/self/fd`-based
exec-from-already-verified-fd to close this window (that approach is
Linux-only, and this stage's production support includes macOS), and it
does not add OS-level sandboxing or privilege separation -- both are out
of scope for P6-B3a.

Because the race is narrowed but not closed, **`soffice_path` and every
directory in its path (including all parent directories) must be deployed
at a trusted, administrator-controlled location that the application's
own runtime user cannot write to.** Concretely, the application's runtime
user must not be able to: modify the `soffice` executable in place,
replace the `soffice` executable, modify the directory that directly
contains it, or replace any parent directory in that path. Any of these
would let a same-host, non-privileged-relative-to-the-app actor swap the
binary inside the revalidation-to-`Popen` window. Violating this
deployment requirement is an **operational/deployment misconfiguration**,
not a defect this stage's identity verification can compensate for.

`ConverterRuntimeIdentity`'s `executable_sha256` and the per-request
revalidation are a defense against the executable being replaced by
another actor who does *not* already have write access to this trusted
location -- they are **not** a defense against an administrator or root
account, which can always replace both the binary and, if it chose to,
the verification logic itself. Mandatory `executable_sha256` narrows the
window in which an unauthorized same-host swap could go undetected; it
does not make executable verification and process exec atomic, and this
stage does not claim otherwise.

## `ProcessRunner`: bounded subprocess execution

`PosixProcessRunner` (`app/services/cv_document_process_runner.py`) is the
only place this stage spawns a real subprocess:

- `Popen(argv, cwd=cwd, env=env, stdout=PIPE, stderr=PIPE, shell=False, start_new_session=True)`
  -- never `shell=True`, `preexec_fn`, `resource.setrlimit`,
  `subprocess.run(capture_output=True)`, or `Popen.communicate()`.
- Two `_PipeReader` threads (stdout/stderr) each drain their pipe to EOF
  in fixed-size chunks, keeping only a bounded prefix and setting a shared
  `overflow_event` once their limit is exceeded. They never call
  `os.killpg`/`proc.wait` and never manage process lifecycle.
- A single **central coordinator**, running in the calling thread, is the
  only place that polls (bounded interval, <=50ms) for process
  completion, observes the overflow event, enforces the deadline, and
  performs SIGTERM -> bounded grace `proc.wait` -> SIGKILL -> bounded final
  `proc.wait` against the whole process group (`os.killpg`) -- never an
  unbounded wait.
- Status precedence: `TERMINATION_FAILED` > `OUTPUT_LIMIT_EXCEEDED` >
  `TIMED_OUT` > `START_FAILED` > `COMPLETED`. An overflow observed in the
  same window as a timeout or a natural exit still yields
  `OUTPUT_LIMIT_EXCEEDED`, provided process-group cleanup itself
  succeeds; a join failure on either reader thread always forces
  `TERMINATION_FAILED`.
- On Windows, `run` always returns `START_FAILED` without ever attempting
  POSIX process-group logic.

`ProcessRunResult` itself -- not only the `_PipeReader` threads that
produce it -- enforces its own output-size invariant: it carries the
`max_stdout_bytes`/`max_stderr_bytes` limits it was built against, and its
own `__post_init__` fail-closed rejects (`ValueError`) any construction
whose `bounded_stdout`/`bounded_stderr` exceed those declared limits, or
whose limits are non-positive, or whose buffers are not `bytes`. It never
silently truncates an out-of-bounds buffer to fit. `PosixProcessRunner`
always propagates its own configured `max_stdout_bytes`/`max_stderr_bytes`
into every `ProcessRunResult` it returns.

## Isolated workspace and secure input write

Each conversion call gets a random workspace via `tempfile.mkdtemp` under
a validated `scratch_root` (mode `0700`, verified as a direct child of
`scratch_root`, never a symlink), with fixed `in`/`out`/`profile`
subdirectories (also `0700`) and fixed `input.docx`/`input.pdf`
filenames -- a caller never supplies a path. `scratch_root` itself is
validated at construction to never be a parent or child of `repo_root`,
`working_copy_root`, or `blob_root`.

The DOCX is written with `O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW` at
mode `0600`, a partial-write loop that handles `os.write` returning `0`,
an `fsync`, and a regular-file `fstat` check -- no raw `OSError` ever
escapes.

## Secure PDF output read and minimal technical validation

`read_pdf_output_securely` performs the same handle-based discipline as
executable identity observation against the converter's PDF output, with
a bounded (5-attempt) retry loop on any observed instability
(`PDF_OUTPUT_CHANGED_DURING_READ` after retries are exhausted). Once a
read is stable, only a minimal technical check is applied: the bytes must
start with `%PDF-` and contain `%%EOF` within the last 1024 bytes -- never
a real PDF parser, never any inspection of CV content.

## Fixed argv and reduced environment

```
soffice_path
-env:UserInstallation=<properly percent-encoded file:// URI for <workspace>/profile>
--headless
--norestore
--convert-to pdf
--outdir <workspace>/out
<workspace>/in/input.docx
```

The `UserInstallation` value is built via
`workspace.profile_dir.resolve(strict=True).as_uri()` -- never a manual
`"file://" + str(path)` concatenation -- so a `scratch_root` containing a
space, a non-ASCII Unicode character, `#`, or `%` is always correctly
percent-encoded rather than silently truncated or misinterpreted (an
unescaped `#`, in particular, would otherwise be read as a URI fragment
delimiter and truncate the path).

The environment passed to `soffice` is a fixed, minimal allowlist --
`PATH`, `HOME`, `LANG`, `LC_ALL` (both locale variables pinned to
`en_US.UTF-8` for deterministic output) -- never the caller's/process's
full environment, never `PYTHONPATH`. A caller never influences argv or
env.

## Conversion outcome mapping

A nonzero exit code is always `CONVERSION_FAILED`; the PDF output is
never read in that case. On a zero exit code, `read_pdf_output_securely`'s
result is mapped exhaustively to a `DocxToPdfConversionStatus`
(`PDF_OUTPUT_MISSING`/`PDF_OUTPUT_LOCKED`/`PDF_OUTPUT_UNREADABLE`/
`PDF_OUTPUT_CHANGED_DURING_READ`/`PDF_OUTPUT_TOO_LARGE`/
`PDF_OUTPUT_INVALID`). Every `ProcessRunStatus` is mapped exhaustively to
its own `DocxToPdfConversionStatus`.

## Cleanup as a security contract

Cleanup always runs in a `finally` block and is verified *before* a
`SUCCEEDED` result is ever built:

- the workspace root's identity (`st_dev`/`st_ino`) is re-checked against
  the identity recorded at setup, it must still be a direct child of
  `scratch_root`, and it must not be a symlink;
- `shutil.rmtree` is used only when the platform's
  `shutil.rmtree.avoids_symlink_attacks` is `True`; `ignore_errors=True`
  is never used;
- the workspace root's absence is verified after `rmtree`.

A cleanup failure after a technically successful conversion becomes
`WORKSPACE_CLEANUP_FAILED` with no PDF bytes ever returned. A cleanup
failure after an earlier failure preserves that earlier status and
appends a bounded cleanup diagnostic -- it never overwrites the primary
failure reason.

## Windows

On Windows, the constructor never calls `observe_executable_identity`
(which is POSIX-only) and `runtime_identity` is always `None`.
`convert_docx_to_pdf` returns `CONVERTER_UNAVAILABLE` immediately, without
ever creating a workspace or starting a subprocess. No Windows `Job
Object` implementation and no Windows real-runtime test exist in this
stage.

## Real-runtime gate

`tests/integration/test_cv_document_libreoffice_real_runtime_smoke.py` is
marked `real_runtime` and excluded from the default backend suite (see
`addopts` in `apps/backend/pyproject.toml`, which now excludes both `eval`
and `real_runtime`). Run it explicitly:

```bash
uv run pytest \
  apps/backend/tests/integration/test_cv_document_libreoffice_real_runtime_smoke.py \
  -m real_runtime -q
```

It builds a real minimal DOCX with `python-docx`, looks for a configured
absolute LibreOffice/soffice path (never a PATH lookup as authority), and
skips with a clear reason when none is found. On success it asserts the
real output starts with `%PDF-` and that the workspace was confirmed
removed. It must never report a real-runtime PASS without an actual
LibreOffice binary present.

## Out of scope for this stage

No `FinalDocxSnapshot` repository wiring, no Confirmed PDF artifact, no
SQL tables, no migration, no current-PDF slot, no replay persistence, no
reconciliation, no API, no router, no frontend, no "Generate PDF" button,
no local file opener. The existing P6-A `CvPdfConversionAdapter`
(`app/services/cv_document_adapters.py`) is untouched by this stage.
