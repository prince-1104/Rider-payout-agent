"""Defense in depth for untrusted text; classification never grants authority.

These conservative patterns detect common attacks, not every possible injection.
Identity, tool arguments, policy and payment authorization remain server-owned even
when text is classified as ordinary data. No model decides its own permissions.
"""

import re
import unicodedata

from pydantic import BaseModel, ConfigDict

from app.domain.policy import normalize_rider_id


class InputClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    flags: tuple[str, ...] = ()

    @property
    def blocked(self):
        return bool(self.flags)


class PromptInjectionDetected(ValueError):
    def __init__(self, flags):
        self.flags = tuple(sorted(set(flags)))
        # Never place raw text, credentials or private records in an exception.
        super().__init__("Untrusted data contains an authority or instruction request")


def normalize_text(text):
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if unicodedata.category(c) != "Cf").casefold()


_PATTERNS = {
    "instruction_override": r"\b(?:ignore|disregard|forget|bypass)\b.{0,40}\b(?:previous|prior|above|all|system|safety)\b.{0,40}\b(?:instructions?|rules?|prompts?|policy|policies)\b",
    "role_spoofing": r"(?:^|\n)\s*(?:system|developer|assistant|admin)\s*:|\[(?:system|developer)\]|<\|(?:im_start|start_header_id)\|>\s*(?:system|developer)|</?(?:system|developer)>",
    "admin_impersonation": r"\b(?:pretend|act|behave|roleplay|you\s+are(?:\s+now)?|i\s+am)\b.{0,35}\b(?:admin(?:istrator)?|system|developer|superuser|ops\s+(?:manager|approver))\b",
    "identity_override": r"\b(?:this\s+is|i\s+am|i'm|use|switch\s+to|act\s+as|impersonate)\s+(?:another|a\s+different)\s+rider\b|\b(?:authenticated_)?rider_id[\"']?\s*(?:=|:)",
    "cross_rider_access": r"\b(?:show|fetch|get|read|access)\b.{0,50}\b(?:another|other|different)\s+rider\b",
    "payment_authorization": r"\b(?:approve|authori[sz]e|force[- ]?pay)\b.{0,30}(?:₹|rs\.?|inr)?\s*\d+|\bapprove\b.{0,30}\b(?:all|every)\b.{0,30}\bdisputes?\b",
    "policy_override": r"\b(?:override|bypass|disable|ignore)\b.{0,40}\b(?:policy|policies|limits?|approval|validation|checks?|authorization)\b",
    "secret_disclosure": r"\b(?:show|reveal|print|dump|repeat|send|disclose|give(?:\s+me)?)\b.{0,60}\b(?:system\s+(?:prompt|instructions?)|developer\s+(?:prompt|instructions?)|api[ _-]?keys?|secrets?|credentials?|environment\s+variables?)\b",
    "internal_tool_request": r"\b(?:call|invoke|execute|run|post(?:\s+to)?)\b.{0,50}(?:\b(?:payswift|create_payout|pay_dispute|get_rider_context|get_trip|find_recent_trips|get_rider_payouts|check_auto_payment_eligibility|reconcile_\w+|internal\s+tools?)\b|/v1/payouts)|\b(?:create_payout|pay_dispute|get_trip|get_rider_payouts)\s*\(",
}
_COMPILED = {flag: re.compile(pattern, re.DOTALL) for flag, pattern in _PATTERNS.items()}


def classify_input(text, authenticated_rider_id=None):
    normalized = normalize_text(text)
    flags = {flag for flag, pattern in _COMPILED.items() if pattern.search(normalized)}
    if authenticated_rider_id is not None:
        identity = normalize_rider_id(authenticated_rider_id)
        if any(normalize_rider_id(rider) != identity for rider in re.findall(r"\br\d+\b", normalized)):
            flags.add("cross_rider_reference")
    return InputClassification(flags=tuple(sorted(flags)))


def guard_untrusted_data(value):
    """Scan nested values and keys without executing, parsing or promoting text."""
    remaining = 20000

    def visit(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if depth > 20 or remaining < 0:
            raise PromptInjectionDetected(["untrusted_data_limit"])
        if isinstance(item, str):
            classification = classify_input(item)
            if classification.blocked:
                raise PromptInjectionDetected(classification.flags)
        elif isinstance(item, dict):
            for key, child in item.items():
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, depth + 1)
    visit(value)


_CREDENTIALS = re.compile(
    r"\b(?:[\w-]*api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)"
    r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", re.IGNORECASE,
)
_SECRET_FIELDS = re.compile(
    r"(?:[\w-]*api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization|"
    r"credentials?|database_url|session_factory|payswift_factory)", re.IGNORECASE,
)


def redact_secrets(value):
    """Best-effort user-supplied credential redaction, in addition to allowlists."""
    if isinstance(value, str):
        value = re.sub(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", "[REDACTED_AUTHORIZATION]", value, flags=re.IGNORECASE)
        value = _CREDENTIALS.sub("[REDACTED_CREDENTIAL]", value)
        value = re.sub(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{8,}\b", "[REDACTED_KEY]", value)
        return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)
    if isinstance(value, dict):
        return {key: "[REDACTED]" if _SECRET_FIELDS.fullmatch(str(key)) else redact_secrets(child)
                for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secrets(child) for child in value]
    return value


def model_data(payload, allowed_fields):
    if not isinstance(payload, dict) or set(payload) != set(allowed_fields):
        raise ValueError("Model input must contain only the stage's approved fields")
    guard_untrusted_data(payload)
    return redact_secrets(payload)


def claim_data(claim):
    """A claim reference requires no rider text, profile, identity or credentials."""
    fields = ("dispute_id", "dispute_type", "trip_id", "dispute_date")
    source = claim if isinstance(claim, dict) else claim.model_dump(mode="json")
    return {field: source.get(field) for field in fields}
