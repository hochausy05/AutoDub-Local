# 06 — UX, Editors & User-Configurable Pipeline

> **Phase rule:** `14_SCOPE_BASELINE.md` defines mandatory MVP vs V1/V2 scope; advanced designs in this document do not imply MVP implementation.

**Design principle:** User controls each stage with sensible defaults, explicit advanced options and feedback. Never require a full end-to-end run to inspect an intermediate result.

## 1. Navigation

- **Project Library:** create, import multiple files, search, tag, sort by status/modified date, disk usage, open/rename/delete (V1: backup/restore).
- **Project Workspace:** top source video player and metadata; left stage navigation `Nguồn video`, `Nhận diện`, `Dịch`, `Giọng đọc`, `Đồng bộ`, `Render`, `Nhật ký`.
- **Settings:** data root, export default, AI/network credentials, hardware profile, cache limits, codecs/voices diagnostics (V1: full backup tools).
- **Queue:** active Jobs, retry/cancel, resources, elapsed/time estimates only when grounded.

## 2. Stage layout contract

Every stage has three areas:

1. Configuration panel (simple controls + Advanced drawer with per-project overrides and Save Preset).
2. Primary working area (player/editor/**simple sync table**/preview, seek linked across panels (V2: multitrack timeline)).
3. Status and history pane (`Not started`, `Running`, `Needs review`, `Ready`, `Stale`, `Failed`) with Job attempts and actionable errors.

Buttons: `Chạy`, `Hủy`, `Chạy lại`, `Lưu nháp`, `Duyệt kết quả`, `Bước tiếp`. Disabled actions explain why. Edits do not vanish when switching tabs.

## 3. Source/import

- Offer `Chọn video`, `Sao chép vào Project` (default) or `Dùng file tại vị trí gốc`; `Chọn thư mục lưu trữ Project`; `Chọn thư mục xuất MP4`.
- Show estimated copy time only when based on measured throughput; show progress in bytes. Browser file picker cannot expose an arbitrary trusted full path; Windows-native file/folder dialog via local backend bridge may be needed for true no-copy mode.
- Display ffprobe results, orientation, VFR, embedded audio tracks and thumbnail; allow relink on missing file.

## 4. ASR editor

- Player with current Segment highlighted; click Segment -> seek; double-click text -> edit with timestamp view; split/merge constrained by positive durations.
- Config: source language, model, device/compute, beam, VAD, optional vocabulary, quality preset.
- Show uncertainty flags; user can run recognition again as a **new Run** without deleting corrected transcript.

## 5. Translation editor

- Table columns: start/end, source, Pass 1, Pass 2, final editable Vietnamese, flags, read fit.
- Multi-row edit -> `Lưu tất cả` using optimistic revision. Undo, restore previous draft, terminology glossary, regenerate selected lines and preview changed count.
- Controls: context genre, style, pronouns, formality, target length, punctuation strategy. Do not force a period at end of every short Unit. Missing/uncertain translations are visibly blocked for TTS unless user approves.
- UI shows Gemini call count/quota stage and explicit consent that text is transmitted to Google.

## 6. TTS editor and player

- Voice selector previews actual chosen voice; default reference voice `vi-VN-NamMinhNeural`; names queried from provider, not invented gender/region descriptions.
- Global rate/pitch/volume and per-Unit override; `Nghe thử` for sample; `Tạo các đoạn chưa có` and `Tạo lại đoạn đã chọn` actions.
- Unit list shows `Chưa tạo`, `Đang tạo`, `Sẵn sàng`, `Lỗi`, `Hết hiệu lực`, with playable artifact as soon as verified.
- Full waveform/seek/time/remaining can be staged after core player; display actual audio duration and target window, highlight overflow.
- Player Play/Pause/Resume, one active audio, source video seek; no surprise background speech.

## 7. AV sync workspace — MVP simple table

- Per-Unit numeric table: original start/end, measured audio duration, allowed gap/offset, proposed placement, overlap/overflow and conflict reason.
- Allowed: edit text and regenerate, bounded Edge-TTS rate adjust, small shift into empty gap, manual numeric placement. Never automatically change original video duration.
- Clicking a Unit seeks video; conflicts remain visible. Approving a plan requires actual measured audio and no hidden truncation.
- **V2 only:** multitrack drag/drop UI, waveform lanes and video stretch/retime. Do not implement these in MVP.

## 8. Render workspace (strong user customization)

- Live preview range, choose `[start, end]`, resolution (source, 720p, 1080p, custom), crop/fit/letterbox/rotation/flip, fps preserve vs CFR, codec/encoder, quality, preset.
- Audio: original volume %/mute/ducking, Vietnamese voice level, fade and clipping guard; show peak/loudness and permit preview.
- Subtitles: Off/SRT/ASS/hardcoded; font, size, color, outline, background, line spacing, safe margins, Y position, optional OCR suggestion.
- Video filters are ordinary editorial choices; no misleading "anti-copyright" mode. User must hold rights to sources they edit.
- Output location/name visible before render; render creates new version, not overwrite prior verified file.
- Job monitor shows encoder path, actual FPS, elapsed, estimated remaining only if calculable from measured progress, warnings and open-output action.

## 9. Batch mode — V1, deferred from MVP

- Each video is a separate Project with a preset snapshot. Select profiles for multiple Projects, queue stages; isolate failure and resource locks.
- Operator can pause Queue or cancel individual Job. Prior successful Projects continue; failed Project remains inspectable.

## 10. Accessibility and responsive behavior

- Keyboard navigation, visible focus, screen-reader labels, no color-only error meanings; actionable tooltips.
- Desktop-first UX with 1440/1920/2560 width, smaller windows functional; avoid hiding destructive actions in ambiguous icons.
- Progress events use stable identifiers and don't reset edit drafts during polling.

## 11. Example task scenarios

**Correct translation only:** Open Project -> Translation -> change 2 lines -> Save All -> TTS shows 2 stale Units -> regenerate selected -> Sync recalculates -> short preview -> new render output.

**Change only subtitles:** Open existing Project -> Render -> change ASS style -> Preview 10s -> Render. No ASR, Gemini or TTS rerun.

**Change output folder:** Settings -> choose destination -> export existing verified MP4 without re-encoding.
