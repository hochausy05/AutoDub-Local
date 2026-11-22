# ADR-002 — MVP scope reset and staged editing

**Status:** Proposed, pending owner acceptance; 2026-11-23. Related: `ADR-001-local-only-and-edge-tts.md`.

**Context:** Original document required complex lease/heartbeat and complete portable backup while the first full dubbing flow was not finished. Advanced multitrack drag/drop and local video stretch add major GUI and drift risk. Keeping original music/SFX may later benefit from speech/vocal separation but its quality and runtime are variable.

**Decision:** MVP uses minimal single-machine durable Jobs, a numeric sync conflict table and source-video-duration-preserving render with mute/gain/ducking. Project portable backup and optional Vocal Separation are V1. Multitrack UI and video retime are V2. All data remain local; Gemini and Edge-TTS remain network providers.

**Consequences:** Faster verified end-to-end MP4; early manual edit controls retained. Separation and advanced editing remain visible in roadmap but cannot block MVP. Basic safety, atomic writes and crash recovery are not optional.
