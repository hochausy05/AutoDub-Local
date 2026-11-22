# RULES — Engineering, Safety, Quality and Documentation

## R01 — Runtime and scope
- Single-host Windows loopback app: backend binds `127.0.0.1`, not `0.0.0.0`, unless the owner explicitly approves a secure LAN mode.
- All persistent project media/metadata must be local. No Supabase or other cloud storage/auth/database dependencies.
- Gemini and Edge-TTS require network access; offline results already cached stay available.

## R02 — Architecture boundaries
- React presentation never runs FFmpeg or directly writes arbitrary server paths.
- API validates, schedules and streams; CPU/GPU-heavy tasks belong to supervised Worker processes.
- Domain objects are independent of FastAPI, SQLite, Edge-TTS and FFmpeg. Provider adapters implement versioned interfaces.
- Immutable Runs + mutable user draft revisions, not overwriting accepted results.

## R03 — Data and paths
- User-selectable `data_root`, `import_mode` (`copy` or `link`) and `output_root` in Settings.
- Never use arbitrary browser-supplied path as trusted server file. Use explicit user selection in a local-only trusted picker, or streamed browser upload to loopback.
- Reject traversal, symlinks escaping managed roots, reserved device names and unsafe extensions; normalize Unicode/long Windows paths.
- Project ID and Run ID determine directories; display filename is separate.
- Atomic write: temporary file -> fsync where appropriate -> validate -> atomic rename -> DB transaction publication.

## R04 — Integrity and concurrency
- SQLite foreign keys ON, WAL on local disks, busy_timeout, schema migrations, single writer policy for Job claims.
- On crash, reconcile running Jobs based on process/lease and existing verified artifacts; never mark a partial artifact READY.
- Cache key includes all meaningful inputs and provider/model/config version. Changes invalidate only true dependents.

## R05 — ASR/translation/TTS safety
- Keep stable timestamps and identity; do not invent transcription or translation content.
- Model output is untrusted. Validate structured JSON, segment coverage, order, revisions, non-empty/explicitly reviewed gaps.
- No silent truncation; long text must be split with lossless mapping or blocked with actionable error.
- TTS transient failures retry with bounded backoff; persistent failure ends in FAILED/REVIEW, not silent WAV disguised as speech.
- Unknown Edge-TTS service behavior/rate limits must not be represented as contractual guarantees.

## R06 — AV sync/render
- Separate audio speed, video speed and subtitle timing controls; do not apply automatic speed changes without a reviewable plan.
- Use integer microseconds/samples for planning; final media measured via FFprobe, not inferred from text length.
- GPU encoding != whole filter graph on GPU. Probe filters/encoder; device/host transfers are explicit and benchmarked.
- Do not offer any claim that mirroring, cropping, color changes or metadata removal defeat Content ID or confer copyright permission. Transformations are optional editing tools for authorized source media.

## R07 — Resources
- Hardware detector reports actual CPU threads, RAM, storage, GPU, VRAM, CUDA/driver and FFmpeg encoder availability.
- No fixed "maximum" workload: preserve OS headroom; tune ASR batch size/concurrency to VRAM & RAM; expose Auto/Balanced/Fast/Custom.
- Cancel must terminate process trees and release handles, locks and scratch files safely.

## R08 — UX
- Every stage: select options -> Run -> review results -> edit -> approve/checkpoint -> next.
- Defaults must be conservative and editable. The user can return to earlier stage without losing accepted revisions.
- Show real source of timing, progress and error state; no fake progress or hidden fallback.

## R09 — Secrets and privacy
- `.env` ignored; use `.env.example` without secrets and Windows credential protection when practical.
- Gemini text and Edge-TTS text are sent to external providers; show this accurately in Settings/privacy notice.
- Do not log API keys, source dialogue, signed URLs, private paths unnecessarily.
- The legacy ZIP contains an `.env`; never copy or publish that file. If a real key was present, owner should rotate it.

## R10 — Testing
- Unit tests for data, parsers, fingerprints, grouping and sync; integration tests for SQLite, media and subprocess lifecycle.
- Acceptance examples must include silence/empty speech, multi-speaker, 9:16, 16:9, variable frame-rate, video without audio, malformed audio, codec fallback and Windows non-ASCII paths.
- Compilation only is not acceptance. Label tests `PASS`, `FAIL`, `NOT RUN`, `BLOCKED`.

## R11 — Dependencies
- Pin versions after compatibility matrix (Python, CTranslate2, CUDA/cuDNN, drivers, FFmpeg, Node); lock files and document hashes.
- Verify current upstream docs before selecting exact versions. Do not blindly transplant old `google-generativeai` SDK.
- Vendored FFmpeg binary must have its applicable license/attribution and update path documented.

## R12 — Cleanup and backup
- Never automatically delete user source videos, final MP4, approved translations or the latest verified TTS artifacts.
- Cleanup is a dry-run inventory with explicit classes, size estimates and confirmation.
- Backup includes a consistent SQLite snapshot (not a naive copy of an active WAL DB) plus project files, checksums, and a tested restore path.

## R13 — Change boundaries
- Stage IDs in `TASK.md` are implementation boundaries; a stage may depend on prior work, but avoid mega-refactors.
- No regression in another stage without documenting diagnosis, tests and explicit rationale.

## R14 — Documentation and language
- `README.md`, `AGENTS.md`, `RULES.md`, `PLAN.md`, `TASK.md`, `UPDATES.md`: Markdown, technical Vietnamese content allowed as appropriate; preserve existing conventions within each file.
- `CHANGELOG.md`: newest-first factual entries in Vietnamese, timestamp UTC+7; do not claim acceptance.
- `ERROR_LOG.md`: real reproducible errors only; never put hypothetical errors in log as incidents.
- Architecture and UI specs: `docs/*.md`, headings, tables, status and stable acceptance IDs.

## R15 — History discipline
- After implementation, append one real task entry at top of `CHANGELOG.md` detailing actual changes/tests/remaining issues.
- `UPDATES.md`: only product-visible working changes; empty/header-only until implemented.
- `TASK.md`: mark only tested completion. `PLAN.md`: revise when actual architecture changes; preserve rationale and prior decisions.
- No invented release versions, screenshots, performance results or benchmark numbers.

## R16 — Stop conditions
- Stop and report if data loss is possible, missing source/output, key leak, timeline inconsistency, unbounded retry, or incorrect AI output mapping.
- After two repeated failed fixes for the same diagnosis, reassess with evidence before proceeding.

## R17 — Product phase boundaries (scope reset)
- `docs/14_SCOPE_BASELINE.md` is the scope authority: MVP = simple fixed-video sync table, single-machine durable Jobs, basic source mix and verified MP4.
- V1 = optional Vocal Separation evaluation, portable checksum backup/restore, enhanced recovery and batch jobs.
- V2 = advanced multitrack drag/drop timeline and video stretch/retime.
- Specs documenting future algorithms do not authorize implementing them in MVP; stage reports must differentiate planned vs implemented.
