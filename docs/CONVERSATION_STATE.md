# Persistent conversation memory (Phase 5)

Run `alembic upgrade head` before starting the service. Migration 0003 creates
`conversations`, `conversation_messages` and `disputes` and backfills previously
stored inbound messages and replies. Conversations are created without requiring
the rider CSV to have been imported; identity comes from the authenticated vendor
request, not from the rider profile table.

Every successful new `POST /messages` stores the rider turn and assistant reply in
one conversation for the request's `rider_id`. They commit atomically with the
existing inbound delivery record and cached reply. Retries replay the original
reply without changing memory; a vendor ID reused under another rider returns 409.
Raw text is never interpreted as a change of identity or structured claim.

`ConversationService(session)` provides:

- `get_or_create_conversation(rider_id)` — one durable conversation per rider.
- `append_message(rider_id, conversation_id=..., message_id=..., text=...,
  received_at=..., role="rider")` — stable sequence numbers, unique vendor ID/role
  pairs and timezone-aware timestamps. History follows append order, even when
  vendor timestamps arrive out of order.
- `get_conversation_history(rider_id, conversation_id=...)` and
  `get_active_disputes(rider_id, conversation_id=...)` — scoped reads.
- `update_conversation_state(rider_id, conversation_id=..., state=ConversationState(...),
  expected_version=...)` — validated structured memory, protected from stale writes.
- `create_dispute`, `update_dispute`, `record_dispute_result` and `resolve_dispute` —
  independent claims, revision checks, calculations from Phase 4 and explicit lifecycle.

Structured state retains pending fields, focused dispute, phase, summary, last
answer and pushback count. A vague first claim can be stored with missing details;
later explicit structured updates add a date or trip, change the claim, or reopen
it after pushback. Clarification preserves the previous claim and calculation in
revision history and clears stale amounts until re-evaluation. Multiple disputes
remain independent. A rejected or answered claim remains active until explicitly
resolved, allowing disagreement and clarification without losing its history.

Example of applying a later date, without interpreting natural-language text:

```python
claim = DisputeRequest.model_validate(stored.claim).model_copy(
    update={"dispute_date": date(2026, 9, 20)}
)
service.update_dispute(
    rider_id, conversation_id=conversation.conversation_id,
    dispute_id=stored.dispute_id, claim=claim,
    expected_revision=stored.revision,
)
session.commit()
```

All mutations flush but leave commit/rollback to the caller. Database uniqueness
and composite ownership constraints protect ordering and identity. Sequence
allocation is atomic; state and dispute updates reject stale versions. Dispute
amount constraints prevent negative or unverified shortfalls from being persisted.

This phase stores memory and structured updates. The webhook still returns its
existing acknowledgement; interpreting dates/order IDs from text and deciding
when to investigate or escalate belong to the later agent phase. No LLM, automatic
claim extraction or payment execution is included.
