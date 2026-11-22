# 10 — Testing, Security, Backup & Crash Recovery

> **Phase rule:** `14_SCOPE_BASELINE.md` defines mandatory MVP vs V1/V2 scope; advanced designs in this document do not imply MVP implementation.

## 1. Quality strategy

Testing layers: pure domain (timestamps, IDs, fingerprints), adapter unit (Gemini/TTS response handling), DB integration (claims/revisions), media integration (FFmpeg/FFprobe), frontend interaction, full end-to-end on 3 licensed local clips. Test on actual Windows and CPU-only environment; NVIDIA path only where hardware exists.

## 2. Test matrix

| ID | Scenario | Expected |
|---|---|---|
| TC-01 | Import .mp4 9:16 and .mkv 16:9 | Accurate streams, seekable local playback |
| TC-02 | Video with VFR | Preserve PTS or explicit CFR policy, no drift |
| TC-03 | Video without audio | ASR flags NO_SPEECH; no Gemini quota wasted |
| TC-04 | Source moved in Link mode | Project indicates missing and requests Relink; no deletion |
| TC-05 | 500 Segments, short fragments | Complete stable mapping, no lost IDs |
| TC-06 | Gemini returns incomplete JSON | Review/fail; no silent source-text substitution |
| TC-07 | User bulk-edits 3 Units | All revisions saved atomically, conflict handled |
| TC-08 | Edge-TTS 2 of 50 Units fail | 48 verified playable, 2 FAILED; Run not READY |
| TC-09 | Long Edge text >250 chars | Full content split or explicit error, never truncated |
| TC-10 | Reopen Project after crash mid-TTS | Reconcile verified Units, retry only missing |
| TC-11 | Change one Unit final text | Only dependent TTS+sync/render stale |
| TC-12 | Overlong audio beyond source window | Conflict visible and reviewable before render |
| TC-13 | Correct voice sample in cache | Same voice/rate reuse; change rate creates new key |
| TC-14 | NVENC absent | CPU x264 chosen and actual reported |
| TC-15 | Subtitle OCR false positive | User can reject location, fallback manual |
| TC-16 | FFmpeg exits 1 or output truncated | No final READY or overwrite of verified output |
| TC-17 | Disk full during import/export | Partial temp cleanup, source/last output intact |
| TC-18 | Two concurrent Jobs require GPU | Serialized resource allocation, UI responsive |
| TC-19 | Windows non-ASCII/long path | FFprobe/FFmpeg works or actionable limitation |
| TC-20 | V1 backup then restore elsewhere | Identical checksums, Project opens and editable |
| TC-21 | Attempt path traversal via API | Request rejected, no arbitrary file read |
| TC-22 | Secret in debug trace | Redaction, no upload, security test |
| TC-23 | Render only subtitle style change | Reuses audio/translation, new verified render |
| TC-24 | Batch 3 videos, one fails | Other Jobs complete, failed result inspectable |

## 3. Recovery design

After launcher restart, reconcile Job rows that were RUNNING. For MVP, compare supervised process/PID, attempt and filesystem; evaluate lease/heartbeat only for V1 multi-worker mode. For each temp artifact validate and either stage candidate or discard *temp only*. If verified immutable artifact exists and fingerprint matches, mark Unit recoverable rather than resynthesize. Persist progress after each Unit. Never treat silent/error audio as READY. A stale `.lock` alone is not proof a process is alive.

## 4. Cancellation

- Cooperative cancellation checks between units/chunks and FFmpeg progress; forcefully terminate only process tree of the current Job after timeout.
- Stop in-flight Edge requests and discard partial `.part` file; maintain successes.
- Cancellation leaves accepted ASR/translation/TTS outputs unchanged and future retry possible.

## 5. Media ownership/security

- Backend binds `127.0.0.1`, rejects unsolicited origins/unsafe methods, local API includes CSRF/origin protections; never assume local web server is automatically secure from malicious websites.
- File selectors must return a validated path from trusted desktop interaction; arbitrary path from request JSON isn't authority.
- Server media endpoints map `project_id + asset_id` to registered files, no unrestricted filesystem browser.
- Only preapproved path roots; symlink/junction resolution, extension + codec checks, path normalization on Windows.
- No secrets in source or logs. Gemini input/Edge speech includes user content transmitted online; disclose to user.

## 6. Backup/restore — V1 quality stage (not MVP)

- Use online SQLite backup, not naive copy of `app.db` while WAL active.
- Bundle manifest + checksums + source/video artifacts with user confirmation about size.
- Recovery import verifies archive sizes/path traversal/zip bombs; staged restoration and new IDs/paths on conflict.
- Manual backup scheduled reminders optional, not remote/scheduled automation by default.

## 7. Testing evidence

For each gate record command, environment (CPU/GPU/driver), fixture, expected/actual, pass/fail/not-run, screenshots/video optional, known limits and the explicit owner acceptance. `CHANGELOG.md` only contains executed work, `ERROR_LOG.md` only observed incidents.

## 8. Licensing and rights

- Declare Edge-TTS network dependency and provider usage constraints; never imply official enterprise SLA.
- Respect Gemini billing/privacy policies; secrets local-only.
- Use properly licensed FFmpeg build, fonts, video assets and source media.
- Do not make any claim that optional mirroring/color/metadata changes authorize reuse or bypass content matching.
