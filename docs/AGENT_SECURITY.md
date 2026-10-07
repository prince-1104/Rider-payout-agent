# Prompt injection and authority boundaries (Phase 8)

Rider messages and persisted text are untrusted data. `prompts/security.txt` is
prepended to each specialist's system prompt. Its instructions prohibit role
redefinition, identity changes, secret disclosure, cross-rider access, direct
internal/provider calls, payment authorization and policy overrides. JSON data
is placed in a human message; data never becomes a system/developer message.

Server-side controls supplement those prompts:

- Before any model or financial/provider call, `classify_input` detects common
  instruction overrides, forged roles, admin impersonation, identity replacement,
  cross-rider references, payment authorization commands, policy overrides, secret
  requests and direct tool calls. Unicode normalization handles compatibility
  characters and invisible formatting characters. Flagged messages get a fixed
  refusal and a zero-amount escalation, not a model-generated financial action.
- Unsafe history is excluded from model context and trip-reference grounding.
  The original message remains in owned conversation storage for audit; models
  receive only safe, necessary history. A subsequent legitimate message can still
  investigate its rider's own claim.
- Each model input has an explicit top-level allowlist. Claim projections omit
  raw rider text, identities and descriptive amounts. Resolution inputs omit
  rider identity and unrelated profile/provider configuration. Recognizable
  user-supplied credentials, authorization headers, secret-valued dictionary
  fields and credential-bearing URLs are redacted. Runtime settings, environment
  variables, session factories and provider credentials are never model context.
- Nested tool values and keys are checked before evidence is consumed. Injected
  commands trigger escalation. The same data guard protects every model input,
  including deterministic investigation evidence. Tool text cannot introduce an
  instruction channel, new tool names/arguments or a permission grant.
- Identity is still bound to trusted application context; claim references must
  belong to that rider and conversation. Strict output schemas forbid identity,
  dispatch-data and amount injection. Investigation follows a bounded complete
  read plan with application-derived arguments. The deterministic engine rereads
  authoritative data. Resolution actions must exactly match server-side policy.
- Payments still require the application-only service to freshly validate the
  owned persisted claim, provider ledger, shortfall, limit and daily reservation
  before POST. Model prose and claimed amounts never authorize money. No model
  receives a PaySwift payout creation tool. Final responses use server facts and
  execution receipts; model explanations cannot reveal system prompts through
  the rider reply or fabricate a completed payout.

Classification is conservative and is not a complete detector for every language
or paraphrase. Security does not depend on matching a keyword: classification
misses remain subject to scoped access, strict schemas, deterministic evidence,
policy validation and guarded financial execution. Redaction is an additional
best-effort safeguard; the primary secret boundary is excluding unnecessary
application data from model inputs.

`classify_input` and refusal/escalation outcomes are durably traced. Agent-step
inputs redact recognizable credentials. `AgentState.security_flags` records safe
classification labels without echoing attack text into replies. These controls
apply to the Phase 7 orchestration entry point; model/vendor webhook wiring is
unchanged. This phase adds no autonomous executor or approval execution endpoint.

Tests load the actual `R037` scenario from `data/conversations.json` and assert
zero payout and no approval. They also cover the seven requested attack types,
Unicode variants, nested tool-output injection, poisoned history, model payload
minimization, credential redaction, fixed system/human roles, classifier misses,
safe follow-up turns and ordinary complaint amounts. All models and provider
calls are mocked. Run `python -m pytest -q`.
