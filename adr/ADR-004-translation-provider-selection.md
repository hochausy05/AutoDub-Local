> **Superseded by PO decision TRANS-02C (2026-11-25):** Cloudflare is retired. Gemini is the sole provider for new Runs. Historical provenance is retained. Local AI is deferred to a separate task. TRANS-02A/B were not accepted.

# ADR-004 — Translation Provider Selection

- **Status:** Accepted by Product Owner for TRANS-02A; provider integration verification remains incomplete.
- **Date:** 2026-11-24

## Context

The existing Translation Engine used Gemini-specific configuration, run metadata, job dispatch, status and consent. Product Owner selected Cloudflare Workers AI as the default and Gemini as a manually chosen secondary provider. Live spend for this task is capped at $0.

## Decision

- Default provider: Cloudflare Workers AI REST `/accounts/{account_id}/ai/run/{model_name}` with `@cf/zai-org/glm-4.7-flash`.
- Secondary provider: Google Gemini Generate Content `v1beta`, `gemini-3.1-flash-lite`; preserve its existing adapter and credential behavior.
- Provider-specific engine strategy identities and adapter construction; one TranslationService/worker, canonical Unit ownership and mapping validation, normalization, checkpoint/persistence and downstream editor/TTS contract remain shared.
- Gemini keeps its Generate Content API, model, versioned Single-Pass prompt, native structured output, retry and quota behavior. Cloudflare keeps its REST route and does not inherit Gemini SDK settings. Provider-specific prompt, wire and chunk policies may diverge only after qualification; unverified candidates remain disabled.
- Provider/model/route/adapter are immutable per Run and Job snapshot. Manual choice applies only to a new Run. Retrying a Run never changes provider.
- There is no automatic fallback. Provider-specific transcript consent is separate; API requests carry `privacy_consent_provider` and the backend rejects a mismatch. Legacy Gemini requests may omit the scope for compatibility. A failed provider call preserves verified Units and reports a stable error.
- Cloudflare JSON Mode is not enabled: GLM-4.7-Flash is absent from the official supported-model list. The current prompt-only JSON compatibility path has not qualified on the 48-Unit synthetic test. Do not enable Compact Text, Thinking controls, new chunk limits or dynamic output budgets without evidence. Strict parsing and exact Unit-ID validation remain mandatory.
- Do not store credentials or request/response text in database, artifacts or diagnostics. REST provider SDK version is `NULL`; record adapter version instead.

## Consequences and evidence limits

Cloudflare and Google remain distinct network boundaries and billing/quota systems, while accepted TranslationRevision and TTS input contracts stay provider-neutral. Existing Gemini Two-Pass/Single-Pass Runs, Jobs, artifacts and accepted revisions remain readable. Schema v23 records immutable provider strategy versions and sanitized request/timeout diagnostics. Cloudflare remains the selected default by PO decision but is not production-qualified by this evidence.

Cloudflare's [model page](https://developers.cloudflare.com/workers-ai/models/glm-4.7-flash/) documents the REST route and `max_completion_tokens`; its [JSON Mode page](https://developers.cloudflare.com/workers-ai/features/json-mode/) does not list this model. The Z.ai [model chat template](https://huggingface.co/zai-org/GLM-4.7-Flash/blob/main/chat_template.jinja) defines `enable_thinking`; Cloudflare's model input schema includes `chat_template_kwargs`. The two synthetic requests sent `enable_thinking=false` and got HTTP 200, but there was no evidence that the Cloudflare runtime changed reasoning behavior. Cloudflare documents native REST `options.rejectIfBusy` and HTTP 429/code 3040 for rejected capacity; GLM-specific acceptance was not tested and the option remains disabled. TRANS-02B observed the REST `result.choices[0].message.content` response envelope. On an encoding-correct synthetic 48-Unit fixture, Compact Text returned 40/48 IDs, included duration fields, and had `finish_reason=stop` (HTTP 200, 18.646 s, 836 input/713 output tokens); strict mapping rejects it. The prompt-only JSON candidate failed JSON parsing (HTTP 200, 28.691 s, 1,181 input/986 output tokens, `finish_reason=stop`). Both used `max_completion_tokens=1024`; neither is production-equivalent to the current 6,000 ceiling. A separate local PowerShell pipe test replaced Chinese characters with `?`; that first fixture's output is excluded from quality evidence. One qualification invocation's tool output was not captured, so its status/usage are unknown. No more than five generation requests were authorized; no further requests were made after these qualification attempts. The failed fixture outcomes do not prove the cause of the prior production timeout or establish that 48 Units is an oversized chunk.
