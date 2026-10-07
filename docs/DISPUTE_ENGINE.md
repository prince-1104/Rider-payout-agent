# Deterministic dispute investigations (Phase 4)

`DisputeService(session).evaluate(authenticated_rider_id, request, reference_date=...)`
reads dispatch and payout records and returns a structured `Dispute`. The pure
equivalent is `app.domain.reconciliation.evaluate_dispute`, which accepts immutable
`TripRecord` and `PayoutRecord` snapshots. Neither path performs network calls or
uses an LLM.

```python
from datetime import date
from app.domain.dispute import DisputeRequest
from app.domain.dispute_types import DisputeType
from app.services.dispute_service import DisputeService

result = DisputeService(session).evaluate(
    authenticated_rider_id="R003",  # Identity established by the caller/vendor.
    request=DisputeRequest(
        dispute_type=DisputeType.MISSING_SURGE,
        trip_id="T926334",
        claimed_amount=300,
    ),
    reference_date=date(2026, 9, 22),  # Trusted receipt/current date, not rider text.
)
# expected_amount=74, actual_amount=49, amount_owed=25, status="valid"
```

Claims support missing/incorrect trip pay, missing surge, missing/incorrect daily
incentives, duplicate/incorrect cancellation penalties, general payouts and unknown
types. A claim can identify one trip or an IST calendar day. For multiple named
trips, investigate each ID separately. Unknown types need review; a vague claim
without a trip/date needs more information. Rider text is never parsed for identity,
dates, amounts or dispatch facts. Expected amounts always use authoritative data.

Policy and reconciliation conventions:

- Trip ownership must match authenticated identity before financial evidence is
  returned. Supplied dates must agree with the trip's recorded IST day.
- Preserve the existing window interpretation: age 0–7 days inclusive; future dates
  and age 8+ are rejected. The reference date is explicit for reproducible tests.
- Use Decimal fare arithmetic and half-up rounding per trip. The existing CSV
  convention treats distances >=100 as meters. Invalid financial data needs review.
- Count distinct completed trips on the rider's IST day. Twelve or more earns ₹150;
  subtract all actual incentive entries, including partial payments.
- Expected penalties are -₹10 for rider cancellations and zero otherwise. Distinct
  extra deductions create a refund; prior compensating credits cap that refund.
- Trip reconciliation includes late postings from other payout dates. Daily general
  claims reconcile the net expected total against the net paid total, including
  overpayments, penalties and incentives. Unattributed/conflicting records need review.
- Claimed amounts are explanatory only. Every payable result is positive and capped
  by its verified expected-minus-actual shortfall. The evidence contains source line
  IDs, trip inputs, completed counts and component calculations.
- UTC export timestamps are used for SQLite storage; naive timestamps are treated
  as UTC. The engine converts to IST explicitly, avoiding SQLite timezone comparisons.

This is an investigation result, not permission to execute a payment. Provider
reconciliation, repeated/overlapping claims, payment idempotency, the ₹200 approval
threshold and once-per-day auto-payment limit belong to the settlement workflow.
The generated dispute ID is stable for the investigation scope, not proof that a
claim has never been paid. The service does not persist disputes or modify records.

`python -m pytest` runs the tests. `tests/test_dispute_engine.py` maps all 24 supplied
conversations to explicit structured claims and verifies their financial amounts
against `data/conversations.json`, both directly from CSV records and through the
database adapter. It does not claim to evaluate conversation handling, payment
execution or approval routing.
