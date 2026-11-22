# ADR-001 — Local-only persistence and Edge-TTS as default

- **Status:** Proposed, pending user acceptance of full blueprint.
- **Date:** 2026-11-23.

## Context
The user decided to start a new project after friction from Supabase Storage/network round trips and desired to reuse the first Streamlit tool's practical pipeline. They want a webapp permitting per-stage manual control, storage directory choice, translation edits and render settings.

## Decision
Use SQLite + local filesystem as the only persistent data layer. UI React/TS, local FastAPI Workers. Default TTS provider is the earlier Edge-TTS approach from the legacy project, behind a provider abstraction. Gemini and Edge-TTS remain online services. No cloud account or Supabase.

## Consequences
- Benefits: local media processing, no cloud file size limits, simpler playback, flexible user-owned directories, easier artifact reuse.
- Costs: explicit backup and crash recovery; translation/Edge speech require Internet; SQLite single-writer discipline; local file access security.
- Alternative rejected: Streamlit monolithic rerun pipeline, and Supabase cloud archive.

## Open
Whether an offline TTS fallback is wanted in a later milestone; do not implement until user chooses.
