# FILE INDEX — AutoDub Local vNext (project root)

**Implemented local application slices** are tracked in `TASK.md` and `CHANGELOG.md`; no stage is owner-accepted unless explicitly stated. All user media stays outside this repository. Current scope: `docs/14_SCOPE_BASELINE.md`.

## Root controls

- `README.md`: status, quick orientation, decisions and root directory map.
- `AGENTS.md`, `RULES.md`: Codex instructions, safety, docs conventions and scope boundaries.
- `PLAN.md`, `TASK.md`: MVP-first milestones and unchecked checklists.
- `CHANGELOG.md`, `UPDATES.md`, `ERROR_LOG.md`: factual status histories (no fake implementation).
- `.gitignore`, `.env.example`: initial safety and config examples.
- `PROJECT_BLUEPRINT.md`: consolidated technical specification (source for detailed report).
- `BAO_CAO_DU_AN_VI.md`: readable Vietnamese overview (source for Vietnamese report).

## Runtime source

- `backend/app/`: FastAPI, SQLite, Jobs/recovery, media, ASR, translation history/editor, TTS and artifact services.
- `backend/tests/`: backend unit and integration tests.
- `frontend/src/`: React UI and local API clients; `scripts/windows/`: Windows development launcher.

## Primary scope and specs

- `docs/14_SCOPE_BASELINE.md`: **MVP/V1/V2 authoritative split**; TRANS-03A currently disables new translation until LOCAL-01A.
- `docs/01_PRODUCT_REQUIREMENTS.md`: MVP product contract.
- `docs/02_SYSTEM_ARCHITECTURE.md`: architecture details.
- `docs/03_DATA_AND_STORAGE.md`: SQLite/artifacts; full portable backup is V1.
- `docs/04_PIPELINE_ALGORITHMS.md`: ASR/translation/TTS/sync algorithms; video retime is V2.
- `docs/05_HARDWARE_PERFORMANCE.md`: CPU/GPU capacity and profiling.
- `docs/06_UI_UX.md`: step-wise editor; sync table MVP / multi-track V2.
- `docs/07_JOBS_AND_API.md`: local Job/API; V1 advanced recovery.
- `docs/08_IMPLEMENTATION_ROADMAP.md`: gates G0–G8 and V1/V2.
- `docs/09_LEGACY_REUSE_AUDIT.md`: reference code audit.
- `docs/10_QA_SECURITY_RECOVERY.md`: test, privacy and backup V1.
- `docs/11_TECH_REFERENCES.md`: references and dependencies.
- `docs/12_USER_CONFIG_CONTRACT.md`: choices per stage.
- `docs/13_RENDER_TIMELINE_SPEC.md`: fixed video MVP, separation V1 and retime V2.

## Architecture and reports

- `assets/architecture.dot/.png`, `assets/pipeline.dot/.png`.
- `docs/reports/AutoDub_Local_vNext_BaoCao_VI.docx/.pdf`.
- `docs/reports/AutoDub_Local_vNext_ChiTiet.docx/.pdf`.
- `adr/ADR-001-local-only-and-edge-tts.md`, `adr/ADR-002-mvp-scope-reset.md`.

No secrets, previous codebase, model weights or real media are included.
