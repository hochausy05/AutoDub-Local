# 03 — SQLite Data Model, Local Storage, Revision & Backup

> **Phase rule:** `14_SCOPE_BASELINE.md` defines mandatory MVP vs V1/V2 scope; advanced designs in this document do not imply MVP implementation.

## 1. Goals

Maintain user edits and processing artifacts across application restarts, crashes and selective reruns, with no Supabase. Never discard accepted data when regenerating. SQLite handles small structured records; large media is stored as local files.

## 2. Tables (logical schema)

| Table | Core columns / invariant |
|---|---|
| `projects` | `id UUID PK`, `name`, `status`, `created_at`, `updated_at`, `active_source_asset_id`, `active_transcription_run_id`, `active_translation_revision_id`, `active_tts_run_id`, `active_sync_plan_id`, `active_render_id` |
| `media_assets` | `id`, `project_id`, `role SOURCE/OUTPUT/PREVIEW`, `import_mode COPY/LINK`, `relative_path` or approved external path, `size`, `sha256`, duration_us, streams JSON, verified_at |
| `transcription_runs` | `id`, `project_id`, `source_asset_id`, model/device/language/VAD config JSON, immutable provenance, status |
| `segments` | `id`, `run_id`, position, start_us, end_us, source_text, confidence/flags; unique(run_id,position), start < end |
| `translation_runs` | Schema v23 is retained in TRANS-03A. Immutable provider/model/route/adapter/provider-strategy/prompt/wire/pass-strategy provenance remains readable for historical Gemini and Cloudflare Runs; no migration or provenance cleanup is performed. New Runs are unavailable pending LOCAL-01A. |
| `translation_units` | `id`, `run_id`, ordered index, start_us/end_us, `source_segment_ids`, proposed_text, final_text, revision, status |
| `translation_edits` | `id`, `unit_id`, old/new text, revision, author=local, timestamp, optional reason |
| `tts_runs` | Implemented by migration v18: immutable accepted-revision/source/config snapshot, producer Job, dependency generations, provider/adapter/synthesis versions and aggregate status. |
| `tts_run_units` | Implemented by migration v18: ordered Unit and source/revision mapping, exact accepted text, timing, synthesis fingerprint, per-Unit state, retryability and verified Artifact reference. A source Segment may appear in more than one Unit when accepted RevisionSegments split a CoverageGroup; each RevisionSegment ID appears once. |
| `tts_preview_cache` | Bounded lookup for verified preview artifacts; previews are separate from translation Unit results. |
| `artifacts` | Shared immutable publication table. TTS uses `tts_audio`, `tts_preview`, and `tts_run_manifest`; migration v19 adds a content fingerprint distinct from the producing Job input fingerprint. |
| `translation_provider_calls` | Per-physical-request provenance includes route, provider strategy/adapter version, per-request latency, total provider-operation latency, Attempt elapsed at capture, request-body bytes, output budget, timeout exception class/phase, actual usage when returned, status and sanitized error code. Credentials and transcript request/response bodies are excluded. |
| `sync_plans` | `id`, `tts_run_id`, policy/config, resolved timeline JSON, conflicts JSON, status, sha256 |
| `render_runs` | `id`, `sync_plan_id`, profile, filtergraph path, output_asset_id, ffprobe report, status |
| `jobs` / `job_attempts` | kind, project, input fingerprint, status, attempts, local_process_owner (V1 may add heartbeat/lease), stage, started/ended, error_code |
| `project_settings` / `app_settings` | typed JSON config with schema version, per-run snapshots; secrets stored separately |
| `artifact_dependencies` | parent fingerprint, child ID; for invalidation and incremental reruns |
| `backup_manifests` | ID, project, created_at, file inventory, content sha256, schema version, completeness |

Relationships: one Project -> multiple source/video Runs -> many Segments/Translation Units/TTS artifacts -> immutable sync plan -> render. Existing Run rows never silently overwrite; active pointers move transactionally after validation.

Migrations v20–v22 widen the translation provider discriminator for Cloudflare, make Google SDK provenance nullable for non-SDK REST calls, and add immutable provider adapter versions. Migration v23 adds immutable provider strategy identity and redacted request/timeout diagnostics; prior rows receive `legacy-shared-v1` and nullable diagnostic fields. It is additive and preserves existing Runs and artifacts.

## 3. Example invariants

- `end_us > start_us >= 0`, `position >= 0`, foreign keys valid. Any overlap is represented and reviewed, not silently collapsed.
- `source_segment_ids` for a Translation Unit is non-empty, ordered, refers to same ASR Run, and the set of active source IDs is completely covered exactly as chosen mapping strategy requires.
- `final_text` edit increments revision by compare-and-swap: `UPDATE ... WHERE id=? AND revision=?`, reject 0 rows as conflict.
- `tts_artifact` READY only if target file exists, decodes, nonzero speech where text nonempty, size/hash/duration match DB.
- TTS Unit READY only after FFprobe confirms MP3/24 kHz/mono, FFmpeg decodes the full stream, measured PCM sample count/duration are recorded, audio is non-silent, and the immutable Artifact is atomically published with SHA-256. A run manifest activates through the existing TTS stage compare-and-swap.
- TTS synthesis fingerprints use stable logical Unit key, exact accepted-text hash, language, voice/rate/pitch/volume, encoding, provider/library and synthesis-policy versions. The accepted revision ID is kept in the immutable Run snapshot but is excluded from per-Unit synthesis fingerprints so unchanged Units remain reusable.
- `render_run` READY only if MP4 exists, playable streams present, reported duration within accepted tolerance, output checksum computed.

## 4. Local path scheme

```text
<User-chosen DataRoot>/
  app.db
  projects/<project_uuid>/
    source/<asset_id>.<ext>
    asr/<run_id>/transcript.json
    translations/<run_id>/snapshot.json
    tts/<run_id>/<unit_id>.<ext>
    sync/<plan_id>/plan.json
    render/<render_id>/preview.mp4
    render/<render_id>/output.mp4
    logs/<job_id>.jsonl
  cache/models/<model_id>/
  cache/preview_tts/<fingerprint>.mp3
  scratch/<job_id>/
  backups/
```

Project source **COPY**: adopt file by streamed copy and probe; source retained in managed project dir. **LINK**: store owner-approved absolute path and fingerprint; show missing status if moved; do not delete linked file even when deleting Project. A `data_root` outside source repo is mandatory. Output export may copy from managed `render/<id>` to a user-selected output folder; record both paths.

## 5. Atomic artifact publication

1. Worker creates `scratch/<job_id>/candidate.<ext>`.
2. Write/flush/close file; independent validation (`ffprobe`, media duration, format, non-silence where required), checksum.
3. Move candidate to immutable `projects/<id>/<kind>/<run_id>/...` within same filesystem when possible.
4. Database transaction adds immutable artifact, updates Job stage/active pointer if input versions still match; commit.
5. Recovery reconciles orphan files/DB rows after crash. Never publish an incomplete artifact.

For cross-volume destination, copy+verify to temp at final volume then atomic rename. Source stays until successful verification.

## 6. SQLite operational policy

- `PRAGMA foreign_keys=ON`; WAL on local SSD/NVMe, `busy_timeout` configured; each process opens its own connection; no shared cross-process connection object.
- Avoid long transactions holding write lock while running inference/network I/O. Claim/update quickly; inference outside transaction.
- For MVP job claiming, acquire a short write transaction (`BEGIN IMMEDIATE`), select eligible work, mark running and create an attempt atomically under one local worker owner. Lease/heartbeat may be added in V1 if multi-worker contention requires it.
- Migrations strictly versioned, backed up before destructive schema changes; no network mounted SQLite WAL file.
- On application close/backup, use SQLite online backup API, ensure consistency of associated files using manifest.

## 7. Version/fingerprint strategy

`fingerprint = SHA256(canonical_json({input_ids+hashes, relevant_config, tool_id, model_version, normalization_version}))`.

Sample invalidations:

| Change | Reuse | Invalidate |
|---|---|---|
| Subtitle font/color | ASR, Translation, TTS, Sync | Render only |
| Voice/rate on Unit 4 | Other Unit TTS artifacts | Unit 4 TTS, Sync, Render |
| Edit Translation Unit 4 | ASR and other Units | Unit 4 TTS + downstream |
| Change ASR model | Source video | New ASR and dependent stages; preserve old Runs |
| Change output path | All processing results | Export file copy only |
| Change background audio mix | ASR/Translation/TTS | Render plan/output |

## 8. Backup/restore — V1 (not a mandatory MVP feature)

- `.autodubpack` (ZIP or folder bundle) includes consistent SQLite project export, source (or references and missing warning), translations, verified TTS, selected render(s), SHA-256 manifest and version metadata.
- If backing up linked external source, prompt Copy-into-backup vs references-only; warn when not portable.
- Restore to staging, verify checksums/schema, resolve ID/path collisions, transactionally activate; never overwrite an existing Project silently.
- When V1 backup is implemented, it must be tested with reopen/play/edit/render on fresh destination. MVP already needs atomic writes and safe startup recovery. No destructive auto-cleanup of original or final results.

## 9. Storage policy

App can report reclaimable `scratch`, previews, obsolete model downloads (with approval), and orphaned failed artifacts. **Never default-delete** source video, final MP4, accepted revision history or currently referenced verified TTS. Old intermediate Runs are only removable through an explicit impact preview and dependency graph.
