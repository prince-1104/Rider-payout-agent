# Deterministic payment authorization (Phase 9)

`app/services/payment_authorization.py` defines the immutable authorization result
and `PaymentAction`: `AUTO_PAY`, `OPS_APPROVAL`, `ESCALATION`, `NO_PAYMENT`.
`authorize_dispute_payment(dispute, rider_id)` revalidates the deterministic
investigation, authenticated ownership and shortfall. The default proposed amount
is `amount_owed`. An optional server-side `amount` proposal is strictly checked:
nonpositive proposals authorize no payment; proposals above the verified balance
are rejected with zero-amount escalation. Unknown, suspicious or malformed
investigations escalate. No model output or free-form prose authorizes money.

Positive verified amounts through ₹200 can auto-pay; larger amounts require ops
approval. A large balance cannot be split into smaller automatic payments.
Processed credits on today's IST calendar day require ops approval. PaySwift does
not reliably distinguish external/manual credits from automatic ones, so all such
credits are conservatively routed to ops. Unresolved payments, historical credits
that cannot be attributed, overlapping prior credits, unavailable provider evidence
and local/provider disagreements require reconciliation rather than a new payment.

Without supplied trusted provider evidence, the function calls PaySwift
`GET /v1/payouts?rider_id=...`. Optional evidence/clock parameters exist for server
composition and tests; this function is never a model-facing tool. A routing result
alone is not an executable payment capability.

```python
from app.db.database import get_session_factory
from app.services.payment_service import PaymentService

result = PaymentService(get_session_factory()).process_dispute(
    authenticated_rider_id=authenticated_rider_id,
    dispute_id=owned_dispute_id,
)
```

`process_dispute` independently reloads owned persisted claims and recomputes their
current shortfall. It obtains a validated PaySwift ledger, applies the shared
authorization function, and:

- Executes only `AUTO_PAY`, reserving a unique rider/day/dispute record with a
  stable reference and generated idempotency key before POST. It persists the
  PaySwift payout ID and confirmed status and records the outcome in a trace.
- Persists `OPS_APPROVAL` using the fresh verified amount. Repeated calls update
  the same pending approval. Pending approval/review records hold execution until
  explicit ops action; they cannot automatically turn into payments after a date
  change or balance correction. No approval-execution endpoint is added.
- Persists an escalation for uncertain or suspicious outcomes. A lost POST
  response retains the original reservation/reference/key; subsequent calls GET
  PaySwift and never blindly issue another POST.
- Returns `NO_PAYMENT` for zero shortfall or an already confirmed payment. A local
  `processed` row alone does not confirm payment. Reconciliation requires an exact
  provider reference/amount match and processed status; ledger disagreement is held.

The existing `pay_dispute` entry point preserves its receipt/exception contract
for the orchestrator. It now uses the same authorization function and creates a
pending approval when auto-payment is denied for an approval route. Advisory
read-only eligibility checks remain advisory. Agent financial routing also uses
the shared authorization function; the model cannot override its decisions.

No public execution method accepts a caller/model-supplied amount or authorization
object. Tests cover ₹0, ₹1, ₹199, ₹200, ₹201, large balances, proposals above owed,
repeat payments, approval holds, immutable decisions, malformed evidence, provider
reconciliation, idempotency, payout IDs, traces and unknown outcomes with mocked
HTTP. Run `python -m pytest -q`.
