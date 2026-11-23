# AGENTS — AutoDub Local vNext

> Entry point for coding agents and Codex. This project is at **design stage** until the user authorizes implementation.

## Mandatory reading order

1. Read this `AGENTS.md` first.
2. Read only the files and sections directly relevant to the current Task ID, including applicable task-specific specs.
3. Read the relevant `RULES.md` rules for that task.
4. Open additional project documents when a dependency or actual conflict requires them; record why they were needed.

Preserve all project invariants, safety rules and scope boundaries while applying this focused reading approach. Conflicting requirements: user instruction > documented accepted ADR > accepted product contract > implementation notes. Log unresolved conflicts instead of guessing.

## Workflow for each coded task

- Identify Task ID (e.g. `LCL-02`), prerequisites, exact scope and acceptance criteria in `TASK.md`.
- Check Git status; protect existing uncommitted changes. Never modify other repos or the legacy archive.
- Audit current local code and current library/API versions; write a concise change plan in the task report.
- Implement the smallest end-to-end vertical slice with typed contracts and tests.
- Run automated tests and relevant manual checks; report unavailable tests honestly.
- Update docs following `RULES.md`, log real errors in `ERROR_LOG.md`, and leave user acceptance unchecked.
- Never auto commit/push, modify external accounts or install a network service without instruction.

## Non-negotiable invariants

1. No Supabase dependencies or cloud storage pipeline.
2. No API key or private media content in logs, repo, screenshots or telemetry.
3. No fake-success TTS: failed speech must be an explicit blocker, not an unlabelled silence clip.
4. No silent truncation of translated text or missing Segment IDs.
5. No automatic destructive cleanup of source videos, accepted edits or verified outputs.
6. Every artifact has stable identity, version fingerprint, provenance and integrity checks.
7. Hardware paths are capability-checked and benchmarked; no unconditional GPU/CPU claims.
8. Render must be checked by FFprobe and compare effective timestamps/sample lengths before publication.
9. Browser/preview/test acceptance must be reported separately from compilation.
10. Build only the requested stage; preserve future-stage interfaces without speculative implementation.

## Suggested commit convention (when approved)

`feat(asr): ...`, `fix(tts): ...`, `docs(arch): ...`. Stage tags in task notes, never claim done because code compiles.

## Scope priority for coding agents

Before implementing or estimating a task, read `docs/14_SCOPE_BASELINE.md`. This is the current MVP/V1/V2 decision and overrides older broader feature suggestions elsewhere. Do not build multitrack UI, video stretch, optional Vocal Separation or complete backup/restore under MVP gates. A minimal persistent local Job lifecycle is still required; complicated heartbeat/lease machinery is not an MVP goal.
