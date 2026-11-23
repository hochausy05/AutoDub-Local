# ARCH-00C — Pipeline Contract Proposal

**Project:** AutoDub-Local

**Status:** PROPOSAL / PENDING REVIEW; G0 NOT ACCEPTED; NOT IMPLEMENTED / NOT TESTED

**Scope:** MVP pipeline contracts: Gemini Translation → Edge-TTS → SyncPlan → Verified MP4. This proposal follows the fixed source-video timeline. It does not include multitrack timeline, video stretch/retime, Vocal Separation, or a distributed worker/lease model.

## 1. Sources and boundary

This proposal narrows the pipeline described by `docs/04_PIPELINE_ALGORITHMS.md` §§3–10 and `docs/13_RENDER_TIMELINE_SPEC.md` §§1–3. It aligns entity identity, accepted revisions, logical Unit keys, fingerprints, CAS activation, and publication with `docs/proposals/ARCH-00A_DATA_CONTRACT_PROPOSAL.md`; Job submission, cancellation, retries, and API errors align with `docs/proposals/ARCH-00B_LOCAL_API_SECURITY_CONTRACT_PROPOSAL.md` §§9–11. Source documents are design contracts, not proof of implementation or provider behavior.

Terms used below:

- **Immutable result:** a completed provider result, verified TTS artifact, accepted SyncPlan, RenderPlan, or published output. A correction creates a new record/artifact; it never mutates the prior result.
- **Editable draft:** a translation edit or SyncPlan override set with `lock_version`, saved by CAS. Accepting a draft creates an immutable accepted snapshot and updates its active pointer in one short DB transaction.
- **Current/active:** a pointer selected by successful activation CAS. A result can be READY and retained but STALE or inactive if its captured input generation/fingerprint or target pointer generation no longer matches.
- **Job success:** the computation finished. It is distinct from artifact integrity (`READY`) and stage activation (`ACTIVE`/`STALE`).

No SQLite transaction is held during provider calls, file hashing, decoding, FFmpeg, or FFprobe. Each Job records an immutable input snapshot and idempotency scope; each attempt is independently recorded. Cancellation and restart follow ARCH-00A/00B Job contracts.

## 2. Shared identity, coverage and mutability invariants

1. Source `Segment.id` identifies an immutable ASR row in its `TranscriptionRun`. Transcript correction/split/merge produces revision rows and explicit lineage; it does not rewrite the source Segment. A translation mapping references ordered revision-segment rows and retains the underlying source Segment IDs.
2. Coverage is checked at two distinct layers: each source `Segment.id` is covered exactly once by a `CoverageGroup` (or explicitly skipped with reason); then each `RevisionSegment` marked `INCLUDED` maps to exactly one `TranslationUnit`. TranslationUnit mapping uses ordered RevisionSegment IDs; source Segment IDs remain provenance through CoverageGroup/revision lineage and are not counted again as TranslationUnit coverage. No missing or duplicate mapping is accepted. Splits/merges record parent lineage; a changed ordered membership creates new logical Unit keys. A final-text edit alone retains the key.
3. Provider output is never made active merely because JSON parsed. It must pass schema, membership, content, coverage, and dependency checks. Missing/invalid output becomes explicit `REVIEW_REQUIRED` or `FAILED`, never an empty success.
4. Provider run/chunk outputs and TTS artifacts are immutable once verified. Draft text is separately mutable under CAS; accepted translation revisions are immutable. Save All either applies every valid edit or none. A failed/stale attempt cannot overwrite an accepted edit or active pointer.
5. An asynchronous Job snapshots accepted revision IDs, unit membership/order, configuration, provider/version identity, dependency fingerprints and expected stage input/pointer generations. Completion verifies these again and uses CAS. If CAS loses a race, keep a verified historical result as STALE where safe; do not activate it.

## 3. C1 — Gemini two-pass Translation

### 3.1 Input contract

```json
{
  "schema_version": 1,
  "project_id": "uuid",
  "transcript_revision_id": "uuid",
  "source_language": "auto",
  "target_language": "vi",
  "context": {
    "summary": "string",
    "glossary": [{"source": "string", "preferred": "string", "note": "string|null"}],
    "style": "string|null"
  },
  "units": [{
    "unit_id": "uuid",
    "logical_unit_key": "uuid",
    "position": 0,
    "revision_segment_ids": ["uuid"],
    "source_text": "string",
    "start_us": 0,
    "end_us": 1000000
  }],
  "config": {
    "provider": "gemini",
    "model_id": "provider model identifier",
    "prompt_version": "string",
    "pass1_schema_version": 1,
    "pass2_schema_version": 1,
    "chunk_policy_version": "string"
  }
}
```

Secrets are resolved outside the snapshot and are never included in it, API output, logs, or fingerprints. Input order is explicit and deterministic. Empty or unsupported source rows must be classified before submitting provider work; they cannot silently disappear.

### 3.2 Structured pass contracts

Both passes request structured JSON with a strict schema (provider-native structured output where available, followed by local strict JSON/schema validation). Unknown fields, duplicate IDs, malformed JSON, invalid Unicode, out-of-range timestamps and extra/missing rows are rejected. Never recover by greedily extracting arbitrary JSON from prose.

Pass 1 request contains the validated input units plus context and asks for a faithful meaning-preserving translation. Example output:

```json
{
  "schema_version": 1,
  "pass": 1,
  "chunk_id": "uuid",
  "items": [{
    "unit_id": "uuid",
    "revision_segment_ids": ["uuid"],
    "translated_text": "string",
    "notes": ["string"]
  }]
}
```

Pass 2 consumes the verified Pass 1 text for the same IDs and asks for natural spoken Vietnamese fitted to the source window while preserving meaning, named entities, numbers, negation and IDs. It may return a review note but cannot change unit membership or timing.

```json
{
  "schema_version": 1,
  "pass": 2,
  "chunk_id": "uuid",
  "items": [{
    "unit_id": "uuid",
    "revision_segment_ids": ["uuid"],
    "optimized_text": "string",
    "review_flags": ["string"]
  }]
}
```

The final TranslationUnit result stores both pass outputs, exact mapping, chunk provenance, validation result and status. Its accepted user-facing text is in a separate `TranslationRevision`/edit snapshot. Provider output itself is not edited in place.

### 3.3 Chunking, reconciliation and partial failure

- Chunk planning is deterministic and bounded by provider context/output limits. Every unit has one output owner chunk. Overlap may repeat read-only context for continuity, but overlapping context rows are not output-owned twice. Reconcile against the complete expected ID set and order; reject duplicate, omitted, unexpected or reassigned IDs.
- If a single unit exceeds the allowed context, use a deliberate semantic subdivision with child mapping/lineage and exact concatenated text; if the subdivision cannot be mapped without loss, mark `REVIEW_REQUIRED`. Never drop an over-limit unit or silently truncate its text.
- Pass 2 for a chunk starts only after the corresponding Pass 1 chunk validates. A failed chunk does not invalidate verified chunks, but the TranslationRun is partial and cannot be accepted as complete until every included unit is READY or explicitly SKIPPED with approved reason.
- Retry only bounded transient provider/network/quota errors. Retry targets failed chunks with the same immutable input and idempotency key; changed input/config creates a new run. Quota exhaustion can enter `WAITING_FOR_QUOTA`; permanent/schema failures are `FAILED` or `REVIEW_REQUIRED` with safe actionable error codes.
- Before accepting, compare the current draft lock and active input generation. CAS conflict returns 409 and preserves both the accepted snapshot and the user's local draft for reconciliation.
- If an accepted edit exists, regeneration never replaces it implicitly. The user must explicitly create a new draft/revision and decide which generated Unit outputs to adopt. A stale provider result remains historical and cannot overwrite a newer accepted revision.

### 3.4 Translation state sketch

`QUEUED → RUNNING → PARTIAL | REVIEW_REQUIRED | WAITING_FOR_QUOTA | FAILED | READY`

`READY` means every required unit has validated Pass 1 and Pass 2 output and exact coverage. User editing/acceptance is a separate revision lifecycle: `DRAFT → ACCEPTED` by CAS; invalid drafts remain editable. `PARTIAL` is observable progress only and is never eligible as the accepted complete translation input to TTS.

## 4. C2 — Edge-TTS per Translation Unit

### 4.1 Input and output

Input is one accepted `TranslationRevision` and its ordered `TranslationUnit` keys/text, plus effective voice, language, rate, supported pitch/volume, output encoding, provider and adapter/synthesis version. Provider credentials are resolved privately and excluded from snapshots/logs. Batch Job captures this input; each Unit synthesis can progress and publish independently.

Successful Unit result:

```json
{
  "schema_version": 1,
  "unit_id": "uuid",
  "logical_unit_key": "uuid",
  "artifact_id": "uuid",
  "fingerprint": "sha256",
  "sha256": "sha256",
  "format": "provider-supported audio format",
  "sample_rate": 24000,
  "sample_count": 123456,
  "duration_us": 5144000,
  "verification": {
    "container_ok": true,
    "decode_ok": true,
    "non_silent_for_nonempty_text": true,
    "verified_by": "verification version"
  },
  "status": "READY"
}
```

`READY` requires a closed/published immutable file, matching hash, supported codec/container, successful decode, positive measured sample count/duration and a non-silence check for non-empty input. Silence detection is a guard against provider failure, not a judgment of speech quality. A Unit may be played as soon as its own artifact is READY while other Units remain queued/running/failed. TTSRun is complete only when all required Units are READY or explicitly skipped by an approved policy; failed Units remain visible and block dependent SyncPlan/render inputs.

### 4.2 Fingerprint and reuse

Fingerprint is versioned and canonical over at least: logical Unit key, exact accepted final-text bytes/hash, language, effective voice/rate/pitch/volume, output format, provider/adapter identity and version, and synthesis pipeline version. Unit identity alone is not sufficient. Use ARCH-00A logical-key semantics: text-only edit may reuse the key but changes fingerprint; split/merge creates new keys with lineage. Reuse only same-project artifact with matching fingerprint/version and current file hash/media verification. A cache hit is a verified reference in the new TTSRun, not a mutable file copy. If any verification fails, mark the candidate corrupt/unavailable and synthesize a new immutable artifact; do not overwrite it.

### 4.3 Long text, retries and errors

- Do not truncate, summarize, normalize away meaningful text, or claim success on provider length failure. Split at sentence/word boundaries into deterministic subchunks that preserve exact concatenated source text and ordering; record chunk ranges and verify reconstruction equals the accepted text exactly. Synthesize all subchunks, join with measured pauses, then verify the assembled Unit artifact. If a lossless bounded split is impossible, set `REVIEW_REQUIRED` and ask for a reviewed shorter edit.
- Transient network/429/5xx may retry with jitter and configured attempt cap. Permanent unsupported voice/config, provider content rejection, decode failure or exhausted retry becomes explicit `FAILED` or `REVIEW_REQUIRED`. `retryable` is returned per error. Retry creates a new JobAttempt; it does not replace READY artifacts or synthesize fake silence.
- Cancellation stops the provider stream/process where supported, closes handles, leaves incomplete scratch unpublishable and retains previously verified outputs. Per-Unit state is independent; batch state is an aggregate and cannot hide a failed Unit.

Unit lifecycle: `QUEUED → RUNNING → VERIFYING → READY`, or `FAILED | REVIEW_REQUIRED | CANCELLED`; retry returns eligible failed Unit to a new attempt. Artifact lifecycle follows ARCH-00A staging/verification/publication and READY invariants.

## 5. C3 — SyncPlan on measured audio and fixed video timeline

### 5.1 Snapshot and calculation

SyncPlan input references an immutable verified source MediaAsset/hash, accepted TranslationRevision, exact ordered per-Unit TTSArtifact IDs/hash/duration/sample count, and sync policy/version. No character-count duration estimate is accepted when verified audio is available. Source Segment/timeline uses integer microseconds; audio placement is converted using `round(t_us * sample_rate / 1_000_000)` and effective FFmpeg PTS/time base is retained for render verification.

Each immutable row contains `logical_unit_key`, `unit_id`, source IDs, source interval `[start_us,end_us)`, measured TTS duration/sample count, proposed onset, permitted shift/rate, effective interval, overlap/overflow values and conflict codes. The plan fingerprint covers source identity/hash, accepted revision/mapping, ordered TTS artifact hashes and measurements, manual timing inputs, sync algorithm/policy version and all configuration that changes placement. Editing creates a new plan revision; no in-place overwrite.

MVP preserves source video start, duration, order, frame timing and VFR PTS; no global duration change, video stretch, retime or multitrack. Supported candidate actions: reviewed text edit followed by real resynthesis; bounded Edge rate candidate followed by measurement; bounded gap shift that preserves order and does not introduce a new overlap. Plan generation itself does not alter media.

### 5.2 Conflicts, review and eligibility

Detect and report at minimum: audio beyond allowed Unit tail (`OVERFLOW`), simultaneous narration (`OVERLAP`), impossible source ordering/negative or invalid intervals (`INVALID_TIMING`), source speaker overlap that conflicts with selected monophonic policy (`SOURCE_OVERLAP`), missing/stale/unverified audio (`MISSING_TTS`/`STALE_INPUT`), clipping risk if measurable, and any interval outside the fixed source duration (`OUT_OF_BOUNDS`). Store explicit tolerance and measured values; do not hide conflict with rounding or silence.

Plan state:

- `DRAFT`: generated candidate, not accepted.
- `REVIEW_REQUIRED`: has a conflict or warning needing user decision.
- `ACCEPTED`: immutable snapshot after all required decisions, CAS against current source/revision/TTS fingerprints and pointer generation.
- `STALE`: dependencies changed after creation/acceptance; retained for history, not renderable.
- `REJECTED`: user discarded it; retained according to history policy.

Default render gate is strict: **any unresolved conflict blocks acceptance and render**. Explicitly accepted overlap/overflow can be renderable only after PO approves a named policy, UI displays warning and listening preview, the row stores the user's decision/reason, and all hard constraints remain satisfied. Invalid timing, missing/stale TTS, out-of-bounds placement and failed verification are never overridable. This default resolves the current spec mismatch: `docs/04_PIPELINE_ALGORITHMS.md` §7 permits manual accept with warning while `docs/13_RENDER_TIMELINE_SPEC.md` §2 requires a defined overlap policy and preview; ARCH-00A §15 defaults to block pending policy approval.

## 6. C4 — Immutable RenderPlan, FFmpeg and Verified MP4

### 6.1 RenderPlan input/output

Render is submitted only for an `ACCEPTED`, current SyncPlan with render-eligible decisions. Snapshot inputs:

```json
{
  "schema_version": 1,
  "project_id": "uuid",
  "source_asset_id": "uuid",
  "source_sha256": "sha256",
  "sync_plan_id": "uuid",
  "sync_plan_fingerprint": "sha256",
  "ordered_tts": [{"artifact_id": "uuid", "sha256": "sha256", "duration_us": 5144000, "sample_count": 123456}],
  "audio_mix": {"source_mode": "mute|gain|duck", "source_gain_db": 0, "narration_gain_db": 0, "duck_policy_version": "string|null"},
  "video": {"fit": "preserve|fit|pad|safe_crop", "fps_policy": "string", "codec": "string", "quality": "string"},
  "subtitles": {"mode": "off|external_srt|burn_in_ass", "artifact_sha256": "sha256|null", "style": {}},
  "ffmpeg_capability_id": "uuid",
  "render_policy_version": "string"
}
```

Actual schema must reject unsupported/unprobed options and store requested and effective encoder/filter settings. Output is a new `RenderRun` plus staged MediaAsset candidate and verification report: container/streams, codec, dimensions, frame rate/time base, rotation, duration, audio sample rate/count/duration, expected subtitle state, FFprobe version/report, full decode smoke result, SHA-256 and verification version. It is not READY until every required check succeeds.

### 6.2 Audio and video behavior

- Video remains on the fixed source timeline; preserve source duration/orientation/aspect unless an explicitly supported safe fit/pad/crop option is selected. No stretch/retime or multitrack.
- Source soundtrack mode is explicit: mute, fixed gain or ducking under narration. Gain/ducking also changes music/effects mixed into the source; expose this limitation. Product defaults and numeric bounds are PO decisions. Narration is placed at SyncPlan sample positions; short boundary fades avoid clicks. Detect peak/clipping and fail or require explicit reviewed setting rather than silently clipping.
- Use argv-based FFmpeg invocation, bounded scratch under managed storage, and `filter_complex_script` for long graphs/Windows command-length constraints. Select only a capability-probed encoder; controlled fallback is allowed only if configured and the actual codec/path is reported. Never claim a GPU path that was not used.
- Cancellation/error closes/reaps the process tree, leaves scratch non-published, retains prior verified MP4 and does not activate partial output.

### 6.3 Verification, fingerprint and publication

Render fingerprint covers source content/hash, SyncPlan fingerprint and placement, ordered TTS hashes, audio mix, subtitle bytes/style, video fit/FPS/codec/quality, effective FFmpeg/filter versions/options and render policy version. Output destination folder is not part of content fingerprint; exporting the same verified result to another approved destination does not require rerender, but destination publication is independently verified.

Render sequence: reserve a unique new destination name; render only to scratch; close and flush; verify with FFprobe and full decode smoke test; compare actual duration, effective video PTS/frame count/time base and audio sample count/duration against RenderPlan within the approved tolerances; verify all streams, nonzero content and checksum; publish immutable without overwrite; then commit artifact row and CAS activation in a short SQLite transaction. Same-volume rename must be create-new/no-replace; cross-volume publication copies to a destination-side temporary file, flushes and rechecks hash/probe, then atomically creates the final unique name. Never delete/replace an earlier verified MP4. Crash reconciliation follows ARCH-00A manifest states; filename existence alone is not proof of READY. Verification or CAS failure keeps a safe historical verified result where possible but never marks an invalid/stale result active.

## 7. Dependency and fingerprint map

| Change | Translation | TTS | SyncPlan | Render |
|---|---|---|---|---|
| Transcript source text/mapping/context used by translation changes | New TranslationRun/fingerprint | stale only if accepted final Unit content/key changes | stale if accepted mapping or timing dependencies change | stale through SyncPlan |
| Edit final text in one Unit | Accepted new translation revision | Reuse matching unchanged Units; changed text fingerprint synthesizes only affected Unit | New plan required | stale |
| Split/merge Unit mapping | New logical keys/lineage and translation revision | New artifacts for affected keys | New plan | stale |
| Voice/rate/output/provider synthesis version changes | unchanged | affected Unit fingerprints change | new plan if effective audio changes | stale |
| Source timing/manual shift/sync policy changes | unchanged unless translation grouping/prompt explicitly depends on it | unchanged | New SyncPlan | stale |
| Subtitle style only | unchanged | unchanged | unchanged | new RenderPlan/render fingerprint |
| Output destination only | unchanged | unchanged | unchanged | content fingerprint unchanged; republish verified output |

Fingerprint serialization is canonical, typed, versioned and deterministic; it includes every effective dependency and excludes secrets, timestamps, destination-only preferences and irrelevant Project metadata. A fingerprint match is necessary but never sufficient for reuse: verify same-project ownership, READY state, file hash/media and dependency snapshot. Artifact IDs and logical keys are not substitutes for fingerprints.

## 8. Failure and recovery rules

| Failure point | Required result |
|---|---|
| Gemini malformed/partial/extra IDs | Reject chunk output; preserve verified chunks and accepted revision; mark explicit partial/review/failure; bounded retry only when transient. |
| Provider quota/network unavailable | `WAITING_FOR_QUOTA` or retryable FAILED; no fake complete TranslationRun; retain user edits. |
| TTS long-text cannot be losslessly chunked | `REVIEW_REQUIRED`; no truncation, silence substitution or READY artifact. |
| TTS provider/decode/speech guard failure | Unit FAILED/REVIEW_REQUIRED; scratch quarantined/removed only by safe temp policy; old artifact remains. |
| Sync dependencies missing or changed | `409 stale_input`/`MISSING_TTS`; no accepted/renderable plan. |
| Unresolved overlap/overflow | Block acceptance/render under default policy; present measured conflict and preview/review affordance. |
| FFmpeg/FFprobe failure, duration/PTS/sample mismatch, corrupt output | Render FAILED; no READY/publish/activation; previous MP4 intact. |
| Crash between file and DB transaction | Reconcile from JobAttempt/manifest; verify fingerprint/hash/media before resume or quarantine; never infer success from path. |
| Cancel races with completion | ARCH-00A Job transaction winner decides; if cancel wins, do not activate; verified historic artifact may remain. |
| Completion loses activation CAS | Commit valid historical result if transaction/evidence valid, mark STALE/inactive; do not overwrite current pointer. |

## 9. Test matrix — all runtime tests NOT RUN

| ID | Stage | Test | Acceptance evidence | Status |
|---|---|---|---|---|
| C00 | Contract | Schema version/unknown fields/Unicode/duplicate JSON keys | Strict reject or accepted canonical payload; no silent coercion | NOT RUN |
| C01 | Translation | CoverageGroup source IDs and TranslationUnit RevisionSegment mapping, incl. contextual chunk overlap | Each source Segment covered once by group; each INCLUDED RevisionSegment mapped once to a Unit; no duplicate/missing/extra IDs | NOT RUN |
| C02 | Translation | Chunk boundary, retry and partial provider failure | Deterministic reconciliation; verified chunk retained; run not complete until coverage satisfied | NOT RUN |
| C03 | Translation | Pass 2 missing/extra ID or invalid schema | Chunk rejected, actionable review/error, no active revision change | NOT RUN |
| C04 | Translation | Edit accepted text while provider job is running | CAS protects accepted edit; old result cannot overwrite, stale result retained safely | NOT RUN |
| C05 | TTS | READY Unit while sibling Units remain queued/running | Playback endpoint exposes only verified READY Unit | NOT RUN |
| C06 | TTS | Same fingerprint reuse; change text/voice/rate/version | Reuse verified matching artifact; synthesize only changed Units | NOT RUN |
| C07 | TTS | Oversize text / sentence and word chunking | Exact source reconstruction and ordering; impossible split is REVIEW_REQUIRED, never truncation | NOT RUN |
| C08 | TTS | 429/network retry cap then permanent decode failure | Bounded attempts, explicit FAILED, no silence/fake READY | NOT RUN |
| C09 | TTS | zero-byte, corrupt, silent non-empty, hash mismatch audio | Verification rejects; no publication or READY | NOT RUN |
| C10 | Sync | measured duration differs from estimate; VFR source | Plan uses measured sample duration and original PTS/time base | NOT RUN |
| C11 | Sync | overlap, overflow, out-of-bounds, missing artifact | Exact conflicts; blocked by default; only approved policy exception can accept eligible conflict | NOT RUN |
| C12 | Sync | Edit timing after plan accepted | New immutable plan/fingerprint; old plan retained but stale; render rejected | NOT RUN |
| C13 | Render | Source mute/gain/duck and narration sample positions | Selected mix encoded, source duration fixed, audio placed at expected sample positions | NOT RUN |
| C14 | Render | FFmpeg fallback, cancellation, process-tree failure | Effective encoder reported; cancellation reaps worker tree; scratch not published | NOT RUN |
| C15 | Render | FFprobe stream, PTS/frame, sample count/duration mismatch | Render FAILED; no READY output or activation | NOT RUN |
| C16 | Publish | Existing verified filename and cross-volume output | New unique immutable output; hash rechecked; existing bytes untouched | NOT RUN |
| C17 | Recovery | Crash before/after rename and DB commit; activation CAS zero rows | Manifest reconcile; no false READY; stale artifact retained/inactive as policy allows | NOT RUN |
| C18 | Scope | Request multitrack/video stretch/Vocal Separation | MVP planner/API rejects or does not expose capability | NOT RUN |

No provider credentials, real provider calls, hardware benchmark, Windows process test, FFmpeg integration test, or runtime/browser test was run for this design proposal.

## 10. Decisions pending Product Owner / Technical Lead

**Product Owner:**

1. Confirm translation source/target language policy, default style/glossary behavior, and whether explicitly skipped source rows are permitted in MVP.
2. Approve manual accept policy for overlap/overflow. Until approved, proposal defaults to blocking acceptance/render for unresolved conflict. If approved, specify hard non-overridable conditions, warning display, listening-preview requirement and required reason/audit fields.
3. Choose default source audio behavior (mute/gain/duck), permitted gain/rate bounds and whether basic subtitle output is in the first MVP render gate.
4. Approve which user action adopts generated text over existing edits and retention/visibility of failed/partial historical runs.
5. Set user-facing output naming/version behavior when a destination already contains a file; proposal always creates a new name and never overwrites verified output.

**Technical Lead / provider review:**

1. Confirm exact Gemini model/version provenance available from provider and whether structured output/JSON schema is supported for the chosen model/API version; define prompt and schema versioning.
2. Set chunk token/output limits, retries/backoff/quota behavior, and data-handling/retention expectations for text sent to Gemini/Edge-TTS.
3. Set Edge-TTS adapter/provider version fingerprint fields, supported codecs, long-text splitting/concatenation format and measurable non-silence/decode tolerances.
4. Define canonical fingerprint encoding, dependency generation boundaries, acceptable TTS reuse verification cost and immutable artifact retention.
5. Approve FFprobe/PTS/sample-length tolerances, decode smoke-test duration, loudness/clipping thresholds, supported codec/filter presets, and atomic no-overwrite publication/recovery evidence on target Windows/filesystems.
6. Verify real hardware/FFmpeg capability and process cancellation behavior on target Windows. Until measured, no GPU/CPU throughput claim is accepted.

## 11. Handoff and gate

Before implementation, resolve the decisions that affect state transitions, render eligibility, source audio behavior and artifact verification; then map these contracts to typed API/domain/storage schemas and implementation slices. First tests should be deterministic boundary tests for translation coverage/schema, draft CAS, TTS fingerprint/reuse and artifact verification, SyncPlan conflict gating, RenderPlan dependency fingerprint, FFprobe mismatch rejection and no-overwrite publication. Only after those pass should real Gemini/Edge-TTS adapters and FFmpeg end-to-end tests be integrated.

**TRANS-01 handoff:** preserve the two-level coverage rule: count source Segment IDs exactly once at `CoverageGroup`; separately require each `RevisionSegment` with status `INCLUDED` to map exactly once to a TranslationUnit. Unit/provider output references RevisionSegment IDs, not a second count of source IDs. This is a data-contract clarification only; ARCH-00D adds no Gemini/provider behavior.

This proposal does not alter canonical specifications, does not claim implementation evidence, and does not accept G0. All runtime tests remain **NOT RUN** pending review and implementation authorization.
