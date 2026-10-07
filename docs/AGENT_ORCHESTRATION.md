# Bounded orchestration (Phase 7)

`Orchestrator` runs a fixed sequence of Conversation, Investigation and Resolution
agents built from LangChain prompt/runnable composition and
[`with_structured_output`](https://reference.langchain.com/python/langchain-core/language_models/chat_models/BaseChatModel/with_structured_output).
Every model response is independently revalidated against a strict Pydantic
schema. There is no autonomous agent executor or free-form financial parsing.

The application constructs a per-request `ToolContext` from authenticated identity
and an aware clock. Models receive JSON views of owned conversation memory, never
the mutable `AgentState`, database sessions, payment services or provider clients.
State identity and conversation identity are immutable.

```python
from datetime import datetime, timezone
from app.agent.orchestrator import Orchestrator
from app.agent.tools import ToolContext
from app.db.database import get_session_factory
from app.services.conversation_service import ConversationService

factory = get_session_factory()
# authenticated_rider_id comes from trusted vendor/application authentication.
context = ToolContext(authenticated_rider_id, factory, datetime.now(timezone.utc))
with factory() as session:
    conversation = ConversationService(session).get_or_create_conversation(context.rider_id)
    conversation_id = conversation.conversation_id
    session.commit()

# model is an application-configured LangChain chat model supporting structured output.
state = Orchestrator(context, model).run(
    conversation_id=conversation_id,
    message_id=vendor_message_id,
    latest_message=vendor_message_text,
)
reply = state.reply
```

The eight stages are:

1. Conversation extracts up to five claim descriptions and missing/multiple flags.
2. Application validation checks owned dispute references, owned trip IDs and
   explicit rider references, rejects duplicate claims and persists revisions.
3. Investigation selects the supplied evidence paths. Application code derives
   every argument and invokes a bounded set of Phase 6 read tools. Its structured
   evidence results contain actual tool responses, not model-generated records.
4. The deterministic dispute engine independently reads authoritative records and
   persists verified results. Model/tool-generated amounts are never authority.
5. Deterministic policy reconciles provider history and application reservations,
   enforces the ₹200/once-per-IST-day rules and chooses all allowed actions.
6. Resolution returns structured recommendations and advisory explanations with
   no amount field. Its IDs, order and actions must exactly match policy.
7. Application code calls the existing guarded payment service or persists an ops
   approval/escalation. Payment policy is checked again against fresh data before
   POST. Unconfirmed outcomes remain held for reconciliation, never blindly retried.
8. Final replies are rendered from engine explanations, verified amounts and
   execution receipts. Model prose is retained in traces but is never parsed or
   used to claim a payment, establish facts or override policy.

Each specialist is called once. The default model-call budget is five; configurable
budgets are limited to 1–5 and checked before each call. There are no automatic
retries. Invalid schemas, missing evidence, provider failures, exhausted budgets or
conflicting resolution recommendations safely queue escalation. Multiple payable
claims can receive at most one automatic payment in a run. Payment reservations
provide the final concurrent-write safeguard across runs.

Every stage and tool call has a durable rider-scoped trace. `AgentState.trace` holds
stage records; `GET /trace/{rider_id}` also includes tool/payment records. Stored
assistant replies replay repeated message IDs without model calls or payment calls.
Message IDs cannot be reused with different text or another rider/conversation.
The state retains owned history, active claims, investigation results, validated
disputes, policy decisions, execution results and the final reply.

Run `alembic upgrade head` to create the ops queue. `GET /ops/pending` now returns
persisted pending items. Approval amounts come from stored engine results;
escalations carry no amount. Items deduplicate by dispute and become resolved when
the application confirms payment or establishes no remaining shortfall. This phase
does not add human approval execution, a dashboard, a model vendor configuration,
an evaluation harness or an autonomous retry worker. The webhook still uses its
existing placeholder until a configured model is connected in application code;
the complete workflow is available through the application entry point above.

Tests use mocked structured models and HTTP transports, including identity/data/
amount injection, policy override, invented tool calls, tool evidence corruption,
claim revisions, multi-dispute routing, replay, provider failures, unknown outcomes,
queue persistence, tracing and budget enforcement. Run `python -m pytest -q`.
