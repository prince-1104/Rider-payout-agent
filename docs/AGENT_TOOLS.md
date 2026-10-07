# Safe agent capabilities (Phase 6)

Install `requirements.txt` and run `alembic upgrade head` before using the tool
layer. There is no LLM or agent loop yet. Tools use LangChain Core's structured
tool interface with strict Pydantic schemas.
The interface follows the [LangChain StructuredTool reference](https://reference.langchain.com/python/langchain-core/tools/structured/StructuredTool).

```python
from datetime import datetime, timezone
from app.agent.tools import ToolContext, build_toolset
from app.db.database import get_session_factory

# Construct this context in trusted HTTP/application code, never from model output.
context = ToolContext(
    authenticated_rider_id=payload.rider_id,
    session_factory=get_session_factory(),
    now=datetime.now(timezone.utc),
)
capabilities = build_toolset(context)
tools = {tool.name: tool for tool in capabilities.read_tools}
audit = tools["reconcile_trip_payout"].invoke({"trip_id": "T926334"})
# For R003: expected ₹74, paid ₹49, shortfall ₹25.
```

The read registry contains rider context, recent trips, owned trip lookup, internal
payout lines, trip fare and incentive calculations, trip/incentive/penalty
reconciliation, stored dispute status, advisory auto-payment eligibility and
today's PaySwift ledger. Model-facing inputs have **no rider_id**. Extra fields,
including impersonation attempts, are rejected even for zero-argument tools.
Foreign trips/disputes are rejected without returning their financial evidence.
Runtime config and metadata cannot replace the context's identity.
Trusted IDs are normalized using the existing application convention (`r3` becomes
`R003`) before queries, provider reads, payments and audit records.

Each tool invocation uses its own ORM session and returns JSON-compatible data.
Both sync and async calls persist a `tool_call` record before execution and its
output afterward. Validation/execution failures also persist an `error` record.
LangChain tool-call IDs are retained in trace inputs when present, alongside the
database invocation ID, so model tool requests can be correlated with their audit.
`GET /trace/{rider_id}` exposes these steps in database order. Traces own their
transactions, so a failed caller transaction does not erase tool history. Audit
writes are the only mutations made by read tools. Tool execution disables external
LangSmith tracing; these financial traces stay in the application database.

`write_tools` is empty. Neither PaySwift creation nor conversation/dispute writes
are exposed to the model. `check_auto_payment_eligibility` is an advisory read
check, not permission to execute a payment.

Trusted application code may separately use:

```python
from app.services.payment_service import PaymentService

receipt = PaymentService(get_session_factory()).pay_dispute(
    authenticated_rider_id=payload.rider_id,
    dispute_id=owned_dispute.dispute_id,
)
```

The application service has no amount argument. It locks owned conversation and
dispute records, recomputes the current shortfall from authoritative data, reads
PaySwift and applies the ₹200 and once-per-IST-day rules. It commits a unique
rider/day and dispute reservation, payment reference and idempotency key before
the external POST. Concurrent attempts cannot reserve a second automatic payment.
An uncertain/rejected provider call is conservatively held for reconciliation;
calling the service again checks the ledger but never issues a second POST.
Lost responses can be reconciled by the stored reference and exact amount.

Overlapping claims, unattributed historical credits, incomplete/malformed provider
records and disagreement between the application/provider ledgers require review.
The service does not infer a missing payment from its own database or split a
large dispute into smaller automatic payments. There is no ops approval execution,
background recovery or payment endpoint in this phase; ambiguous outcomes stay
held for later human/application reconciliation.

Tests mock PaySwift through httpx and cover every tool, strict input schemas,
identity injection, cross-rider access, sync/async invocation traces, CSV-derived
calculations, provider errors and payment safety. No real payments are sent by tests.
