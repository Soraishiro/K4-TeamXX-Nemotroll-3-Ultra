"""Deterministic dispute policy engine: evidence facts -> decision -> verified decision.

Pure functions only (no MCP / trace I/O) so every rule is unit-testable offline.

Evidence mixes the case's own records with rows dated outside the case window
(before the purchase or after the case was opened). Those rows cannot describe this
dispute, so facts are built only from in-window rows and the rest are reported as
resolved data conflicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

MONEY_TOLERANCE_BRL = 0.01
WINDOW_LEAD = timedelta(days=1)
# Charges and reconciliation happen at checkout; later captures belong to other orders.
CHECKOUT_WINDOW = timedelta(days=2)

ISSUE_PRIORITY = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "refund_failed",
    "refund_pending",
    "payment_mismatch",
    "duplicate_charge",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
)

# Mirrors the EC_POLICY_V1 rules served by get_policy; used when the policy call fails.
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "canceled_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "party_types": ["platform"],
    },
    "unavailable_order_paid": {
        "case_status": "action_required",
        "recommended_action": "issue_refund",
        "party_types": ["seller"],
    },
    "late_delivery_seller": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "party_types": ["seller"],
    },
    "late_delivery_logistics": {
        "case_status": "action_required",
        "recommended_action": "refund_freight",
        "party_types": ["logistics_provider"],
    },
    "valid_split_payment": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "party_types": ["customer"],
    },
    "payment_mismatch": {
        "case_status": "action_required",
        "recommended_action": "reconcile_payment",
        "party_types": ["payment_provider"],
    },
    "duplicate_charge": {
        "case_status": "action_required",
        "recommended_action": "refund_duplicate_charge",
        "party_types": ["payment_provider"],
    },
    "refund_pending": {
        "case_status": "needs_investigation",
        "recommended_action": "monitor_refund",
        "party_types": ["payment_provider"],
    },
    "refund_failed": {
        "case_status": "action_required",
        "recommended_action": "retry_refund",
        "party_types": ["payment_provider"],
    },
    "unsupported_claim": {
        "case_status": "no_action",
        "recommended_action": "document_no_action",
        "party_types": ["customer"],
    },
    "insufficient_evidence": {
        "case_status": "needs_investigation",
        "recommended_action": "request_additional_evidence",
        "party_types": ["unknown"],
    },
}

CAUSE_CODES = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_PAYMENT",
    "unavailable_order_paid": "PRODUCT_UNAVAILABLE_AFTER_PAYMENT",
    "late_delivery_seller": "SELLER_MISSED_SHIPPING_LIMIT",
    "late_delivery_logistics": "CARRIER_DELIVERY_DELAY",
    "valid_split_payment": "SPLIT_PAYMENT_MATCHES_ORDER_TOTAL",
    "payment_mismatch": "PAYMENT_RECONCILIATION_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "refund_pending": "REFUND_PENDING",
    "refund_failed": "REFUND_PROCESSING_FAILED",
    "unsupported_claim": "CLAIM_NOT_SUPPORTED_BY_EVIDENCE",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
}

FULL_REFUND_ISSUES = {"canceled_order_paid", "unavailable_order_paid"}

REFUND_ACTIONS = {
    "issue_refund",
    "refund_freight",
    "refund_duplicate_charge",
    "retry_refund",
    "reconcile_payment",
}


# --------------------------------------------------------------------------- parsing


def to_money(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def to_datetime(value: Any) -> datetime | None:
    """Parse ISO timestamps; aware values are normalised to naive UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    return None


def _rows(data: Any, key: str | None = None) -> list[dict[str, Any]]:
    if isinstance(data, dict) and key is not None:
        data = data.get(key)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def _field(data: Any, *keys: str) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return None


# --------------------------------------------------------------------------- facts


@dataclass
class Item:
    item_id: str | None
    seller_id: str | None
    price: float
    freight: float
    shipping_limit: datetime | None


@dataclass
class Capture:
    amount: float
    at: datetime | None
    reference: str | None = None


@dataclass(frozen=True)
class MoneyEvent:
    event_type: str
    status: str
    amount: float
    at: datetime | None


@dataclass
class Facts:
    order_id: str | None
    has_order: bool
    order_status: str | None
    purchased_at: datetime | None
    carrier_at: datetime | None
    delivered_at: datetime | None
    estimated_at: datetime | None
    items: list[Item]
    captures: list[Capture]
    payment_events: list[MoneyEvent]
    refund_events: list[MoneyEvent]
    payments_known: bool = True
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def items_total(self) -> float | None:
        if not self.items:
            return None
        return round(sum(item.price + item.freight for item in self.items), 2)

    @property
    def freight_total(self) -> float:
        return round(sum(item.freight for item in self.items), 2)

    @property
    def captured_total(self) -> float:
        return round(sum(capture.amount for capture in self.captures), 2)

    @property
    def seller_ids(self) -> list[str]:
        return list(dict.fromkeys(item.seller_id for item in self.items if item.seller_id))

    @property
    def item_ids(self) -> list[str]:
        return list(dict.fromkeys(item.item_id for item in self.items if item.item_id))

    @property
    def payment_references(self) -> list[str]:
        return list(dict.fromkeys(c.reference for c in self.captures if c.reference))

    @property
    def latest_shipping_limit(self) -> datetime | None:
        limits = [item.shipping_limit for item in self.items if item.shipping_limit]
        return max(limits) if limits else None


class _Window:
    """[purchase - lead, case opened]; open-ended on any side that is unknown."""

    def __init__(self, start: datetime | None, end: datetime | None) -> None:
        self.start = start - WINDOW_LEAD if start else None
        self.end = end

    def contains(self, moment: datetime | None) -> bool:
        if moment is None:
            return True
        if self.start and moment < self.start:
            return False
        return not (self.end and moment > self.end)


def extract_facts(
    order_id: str | None,
    order_findings: dict[str, Any],
    payment_findings: dict[str, Any],
    shipment_findings: dict[str, Any],
    opened_at: str | None = None,
) -> Facts:
    order = order_findings.get("order_data")
    shipment = shipment_findings.get("shipment_data")

    def when(order_keys: tuple[str, ...], shipment_keys: tuple[str, ...]) -> datetime | None:
        return to_datetime(_field(order, *order_keys)) or to_datetime(
            _field(shipment, *shipment_keys)
        )

    purchased_at = when(("order_purchase_timestamp",), ("purchased_at",))
    window = _Window(purchased_at, to_datetime(opened_at))
    excluded: dict[str, int] = {}

    approved_at = when(("order_approved_at",), ("approved_at",)) or purchased_at
    checkout_end = approved_at + CHECKOUT_WINDOW if approved_at else None
    if checkout_end and window.end:
        checkout_end = min(checkout_end, window.end)
    checkout = _Window(purchased_at, checkout_end or window.end)

    def keep(source: str, moment: datetime | None, scope: _Window = window) -> bool:
        inside = scope.contains(moment)
        if not inside:
            excluded[source] = excluded.get(source, 0) + 1
        return inside

    items = [
        Item(
            item_id=str(row["order_item_id"]) if row.get("order_item_id") is not None else None,
            seller_id=str(row["seller_id"]) if row.get("seller_id") else None,
            price=to_money(row.get("price")) or 0.0,
            freight=to_money(row.get("freight_value")) or 0.0,
            shipping_limit=to_datetime(row.get("shipping_limit_date")),
        )
        for row in _rows(order_findings.get("items_data"), "items")
    ]
    items = [item for item in items if keep("order_items", item.shipping_limit)]

    timeline = payment_findings.get("payment_timeline_data")
    payment_rows = _rows(payment_findings.get("payments_data"), "payments") or _rows(
        timeline, "payments"
    )
    events = [
        MoneyEvent(
            event_type=str(row.get("event_type", "")).lower(),
            status=str(row.get("status", "")).lower(),
            amount=to_money(row.get("amount_brl", row.get("amount"))) or 0.0,
            at=to_datetime(row.get("event_at")),
        )
        for row in _rows(timeline, "events")
    ]
    events = [event for event in events if keep("payment_events", event.at, checkout)]
    unique_events = list(dict.fromkeys(events))
    if len(unique_events) < len(events):
        # Byte-identical rows (same moment, type and amount) are one record served twice.
        excluded["payment_events"] = (
            excluded.get("payment_events", 0) + len(events) - len(unique_events)
        )
        events = unique_events

    if events:
        captures = [Capture(e.amount, e.at) for e in events if e.event_type == "captured"]
    else:
        # No timeline to date the captures: every payment row is all we have.
        captures = [
            Capture(amount, None)
            for amount in (to_money(row.get("payment_value")) for row in payment_rows)
            if amount is not None
        ]
    _attach_payment_references(order_id, captures, payment_rows)
    if len(payment_rows) > len(captures) and events:
        excluded["payments"] = len(payment_rows) - len(captures)

    refund_events = [
        MoneyEvent(
            event_type=str(row.get("event_type", "")).lower(),
            status=str(row.get("status", row.get("refund_status", ""))).lower(),
            amount=to_money(row.get("amount_brl", row.get("amount"))) or 0.0,
            at=to_datetime(row.get("event_at")),
        )
        for row in _rows(payment_findings.get("refund_data"), "events")
    ]
    refund_events = [event for event in refund_events if keep("refund_events", event.at)]

    for row in _rows(shipment, "events"):
        keep("shipment_events", to_datetime(row.get("event_at")))

    status = _field(order, "order_status") or _field(shipment, "order_status")
    facts = Facts(
        order_id=order_id,
        has_order=order is not None,
        order_status=str(status).lower() if status else None,
        purchased_at=purchased_at,
        carrier_at=when(("order_delivered_carrier_date",), ("delivered_carrier_at",)),
        delivered_at=when(("order_delivered_customer_date",), ("delivered_customer_at",)),
        estimated_at=when(("order_estimated_delivery_date",), ("estimated_delivery_at",)),
        items=items,
        captures=captures,
        payment_events=events,
        refund_events=refund_events,
        payments_known=bool(payment_rows or events),
    )
    facts.conflicts = _conflicts(order, shipment, excluded)
    for findings in (order_findings, payment_findings, shipment_findings):
        facts.warnings.extend(findings.get("warnings", []))
    return facts


def _attach_payment_references(
    order_id: str | None, captures: list[Capture], payment_rows: list[dict[str, Any]]
) -> None:
    """Link each capture to one payment row of the same amount (order_id:payment_sequential)."""
    unused = list(payment_rows)
    for capture in captures:
        for row in unused:
            if to_money(row.get("payment_value")) == capture.amount:
                capture.reference = f"{order_id}:{row.get('payment_sequential', '1')}"
                unused.remove(row)
                break


CONFLICT_SOURCES = {
    "order_items": (["get_order_items", "get_order"], "get_order"),
    "payments": (["get_order_payments", "get_payment_timeline"], "get_payment_timeline"),
    "payment_events": (["get_payment_timeline", "get_order"], "get_order"),
    "refund_events": (["get_refund_timeline", "get_order"], "get_order"),
    "shipment_events": (["get_shipment_summary", "get_order"], "get_order"),
}


def _conflicts(order: Any, shipment: Any, excluded: dict[str, int]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for name in CONFLICT_SOURCES:
        if excluded.get(name):
            sources, selected = CONFLICT_SOURCES[name]
            conflicts.append(
                {
                    "field": name,
                    "sources": sources,
                    "selected_source": selected,
                    "resolution_code": "EXCLUDED_RECORDS_OUTSIDE_CASE_WINDOW",
                }
            )
    order_delivered = to_datetime(_field(order, "order_delivered_customer_date"))
    shipment_delivered = to_datetime(_field(shipment, "delivered_customer_at"))
    if order_delivered and shipment_delivered and order_delivered != shipment_delivered:
        conflicts.append(
            {
                "field": "delivered_customer_date",
                "sources": ["get_order", "get_shipment_summary"],
                "selected_source": "get_shipment_summary",
                "resolution_code": "UNRESOLVED_DELIVERY_DATE_MISMATCH",
            }
        )
    return conflicts[:5]


def unresolved_conflicts(facts: Facts) -> int:
    return sum(c["resolution_code"].startswith("UNRESOLVED") for c in facts.conflicts)


# --------------------------------------------------------------------------- issues


@dataclass
class Finding:
    issue: str
    refund_lines: list[dict[str, Any]]


def _money_eq(left: float, right: float) -> bool:
    return abs(left - right) <= MONEY_TOLERANCE_BRL


def detect_findings(facts: Facts) -> list[Finding]:
    """Every issue the in-window evidence supports, in ISSUE_PRIORITY order."""
    found: dict[str, Finding] = {}
    paid = facts.captured_total
    was_paid = paid > 0 or not facts.payments_known

    def full_refund(reason: str) -> list[dict[str, Any]]:
        if not facts.captures:
            return []
        return [{"reason_code": reason, "amount_brl": paid, "entity_id": facts.order_id}]

    if facts.order_status == "canceled" and was_paid:
        found["canceled_order_paid"] = Finding(
            "canceled_order_paid", full_refund("CANCELED_ORDER_REFUND")
        )
    if facts.order_status == "unavailable" and was_paid:
        found["unavailable_order_paid"] = Finding(
            "unavailable_order_paid", full_refund("UNAVAILABLE_ORDER_REFUND")
        )

    refund_statuses = {event.status for event in facts.refund_events}
    if "failed" in refund_statuses:
        failed = [e for e in facts.refund_events if e.status == "failed"]
        found["refund_failed"] = Finding(
            "refund_failed",
            [
                {
                    "reason_code": "FAILED_REFUND_RETRY",
                    "amount_brl": event.amount,
                    "entity_id": facts.order_id,
                }
                for event in failed
                if event.amount > 0
            ],
        )
    elif "pending" in refund_statuses:
        found["refund_pending"] = Finding("refund_pending", [])

    mismatches = [e for e in facts.payment_events if "mismatch" in e.event_type]
    if mismatches:
        found["payment_mismatch"] = Finding(
            "payment_mismatch",
            [
                {
                    "reason_code": "PAYMENT_MISMATCH_REFUND",
                    "amount_brl": event.amount,
                    "entity_id": facts.order_id,
                }
                for event in mismatches
                if event.amount > 0
            ],
        )

    duplicates = _duplicate_captures(facts)
    if duplicates:
        found["duplicate_charge"] = Finding(
            "duplicate_charge",
            [
                {
                    "reason_code": "DUPLICATE_CHARGE_REFUND",
                    "amount_brl": capture.amount,
                    "entity_id": capture.reference or facts.order_id,
                }
                for capture in duplicates
            ],
        )

    late = bool(
        facts.delivered_at
        and facts.estimated_at
        and facts.delivered_at.date() > facts.estimated_at.date()
    )
    if late:
        limit = facts.latest_shipping_limit
        seller_late = bool(limit and facts.carrier_at and facts.carrier_at > limit)
        issue = "late_delivery_seller" if seller_late else "late_delivery_logistics"
        freight = facts.freight_total
        amount = min(freight, paid) if paid > 0 else freight
        found[issue] = Finding(
            issue,
            [
                {
                    "reason_code": "LATE_DELIVERY_FREIGHT_REFUND",
                    "amount_brl": amount,
                    "entity_id": facts.item_ids[0] if facts.item_ids else facts.order_id,
                }
            ]
            if amount > 0
            else [],
        )

    if len(facts.captures) > 1 and not duplicates:
        found["valid_split_payment"] = Finding("valid_split_payment", [])

    return [found[issue] for issue in ISSUE_PRIORITY if issue in found]


def _duplicate_captures(facts: Facts) -> list[Capture]:
    """Repeat captures of the same amount that together exceed the order total.
    Equal parts that add up to the order total are a legitimate split payment."""
    by_amount: dict[float, list[Capture]] = {}
    for capture in facts.captures:
        if capture.amount > 0:
            by_amount.setdefault(capture.amount, []).append(capture)
    repeats = [group for group in by_amount.values() if len(group) > 1]
    if not repeats:
        return []
    total = facts.items_total
    if total is not None and _money_eq(facts.captured_total, total):
        return []
    return [capture for group in repeats for capture in group[1:]]


# --------------------------------------------------------------------------- decision


@dataclass
class Decision:
    primary_issue: str
    case_status: str
    confidence: float
    ranked_causes: list[dict[str, Any]]
    responsible_parties: list[dict[str, Any]]
    refund_lines: list[dict[str, Any]]
    recommended_refund_brl: float
    resolution_actions: list[str]
    data_conflicts: list[dict[str, Any]]
    claim_verdicts: dict[str, tuple[str, float]]
    secondary_issues: list[str]


def policy_rules(policy_data: Any) -> dict[str, dict[str, Any]]:
    """Normalise get_policy rules to {issue: {case_status, recommended_action, party_types}}."""
    rules = {issue: dict(rule) for issue, rule in DEFAULT_RULES.items()}
    served = policy_data.get("rules") if isinstance(policy_data, dict) else None
    if not isinstance(served, dict):
        return rules
    for issue, rule in served.items():
        if not isinstance(rule, dict) or issue not in rules:
            continue
        if rule.get("case_status"):
            rules[issue]["case_status"] = rule["case_status"]
        if rule.get("recommended_action"):
            rules[issue]["recommended_action"] = rule["recommended_action"]
        party_types = [
            party.get("party_type")
            for party in rule.get("responsible_parties", [])
            if isinstance(party, dict) and party.get("party_type")
        ]
        if party_types:
            rules[issue]["party_types"] = list(dict.fromkeys(party_types))
    return rules


def decide(
    facts: Facts,
    claims: list[dict[str, Any]],
    evidence_completeness: float,
    policy_data: Any = None,
) -> Decision:
    """Pick the primary issue from evidence. Claim topics only break ties between issues
    the evidence already supports; the customer message is never ground truth."""
    rules = policy_rules(policy_data)
    claim_topics = [str(claim.get("topic", "")) for claim in claims]

    if not facts.has_order:
        primary, secondary = Finding("insufficient_evidence", []), []
    else:
        findings = detect_findings(facts)
        if findings:
            primary = next((f for f in findings if f.issue in claim_topics), findings[0])
            secondary = [f for f in findings if f is not primary]
        else:
            primary, secondary = Finding("unsupported_claim", []), []

    rule = rules[primary.issue]
    parties = [
        {"party_type": party_type, "party_id": seller_id}
        for party_type in rule["party_types"]
        for seller_id in (facts.seller_ids or [None] if party_type == "seller" else [None])
    ]
    causes = [CAUSE_CODES[primary.issue]] + [CAUSE_CODES[f.issue] for f in secondary]
    decision = Decision(
        primary_issue=primary.issue,
        case_status=rule["case_status"],
        confidence=0.0,
        ranked_causes=[
            {"cause_code": code, "rank": rank} for rank, code in enumerate(causes[:5], 1)
        ],
        responsible_parties=parties,
        refund_lines=primary.refund_lines,
        recommended_refund_brl=round(sum(line["amount_brl"] for line in primary.refund_lines), 2),
        resolution_actions=[rule["recommended_action"]],
        data_conflicts=facts.conflicts,
        claim_verdicts={},
        secondary_issues=[f.issue for f in secondary],
    )
    decision.claim_verdicts = _claim_verdicts(decision, claims)
    decision.confidence = calibrate(decision, facts, claim_topics, evidence_completeness)
    return decision


def _claim_verdicts(
    decision: Decision, claims: list[dict[str, Any]]
) -> dict[str, tuple[str, float]]:
    verdicts: dict[str, tuple[str, float]] = {}
    for claim in claims:
        claim_id = str(claim.get("claim_id", "claim-unknown"))
        topic = str(claim.get("topic", ""))
        if decision.primary_issue == "insufficient_evidence":
            verdicts[claim_id] = ("insufficient_evidence", 0.4)
        elif topic == "requested_full_refund":
            refund = decision.recommended_refund_brl
            if decision.primary_issue in FULL_REFUND_ISSUES and refund > 0:
                verdicts[claim_id] = ("supported", 0.85)
            elif refund > 0:
                verdicts[claim_id] = ("partially_supported", 0.75)
            else:
                verdicts[claim_id] = ("unsupported", 0.8)
        elif topic == "unsupported_claim":
            verdicts[claim_id] = ("unsupported", 0.8)
        elif topic == decision.primary_issue:
            verdicts[claim_id] = ("supported", 0.9)
        elif topic in decision.secondary_issues:
            verdicts[claim_id] = ("partially_supported", 0.6)
        else:
            verdicts[claim_id] = ("unsupported", 0.8)
    return verdicts


def calibrate(
    decision: Decision, facts: Facts, claim_topics: list[str], evidence_completeness: float
) -> float:
    """Confidence from evidence quality: completeness of the evidence the conclusion needs,
    ambiguity between competing issues, and unresolved conflicts. Never above 0.95.
    Out-of-window records are excluded deterministically, so they do not lower it."""
    if decision.primary_issue == "insufficient_evidence":
        return 0.3
    confidence = 0.9 if decision.primary_issue != "unsupported_claim" else 0.8
    confidence -= 0.3 * (1.0 - max(0.0, min(1.0, evidence_completeness)))
    if decision.secondary_issues and decision.primary_issue not in claim_topics:
        confidence -= 0.15
    elif decision.secondary_issues:
        confidence -= 0.05
    confidence -= 0.1 * unresolved_conflicts(facts)
    confidence -= 0.02 * min(len(facts.warnings), 5)
    return round(max(0.05, min(0.95, confidence)), 2)


# --------------------------------------------------------------------------- verifier


def verify(decision: Decision, facts: Facts) -> list[str]:
    """Enforce cross-field invariants in place; return a code for every correction made."""
    fixes: list[str] = []

    lines = [line for line in decision.refund_lines if line["amount_brl"] > 0][:10]
    if len(lines) != len(decision.refund_lines):
        fixes.append("DROPPED_ZERO_REFUND_LINES")
    paid = facts.captured_total
    line_total = sum(line["amount_brl"] for line in lines)
    if paid > 0 and line_total > paid + MONEY_TOLERANCE_BRL:
        scale = paid / line_total
        lines = [{**line, "amount_brl": round(line["amount_brl"] * scale, 2)} for line in lines]
        fixes.append("REFUND_CAPPED_AT_AMOUNT_PAID")
    decision.refund_lines = lines
    total = round(sum(line["amount_brl"] for line in lines), 2)
    if not _money_eq(total, decision.recommended_refund_brl):
        fixes.append("REFUND_TOTAL_RECOMPUTED_FROM_LINES")
    decision.recommended_refund_brl = total

    if decision.case_status in {"no_action", "needs_investigation"} and total > 0:
        decision.refund_lines, decision.recommended_refund_brl = [], 0.0
        fixes.append("NO_REFUND_WITHOUT_ACTION")
    has_refund_action = any(action in REFUND_ACTIONS for action in decision.resolution_actions)
    if decision.recommended_refund_brl > 0 and not has_refund_action:
        decision.resolution_actions.insert(0, "issue_refund")
        fixes.append("ADDED_REFUND_ACTION")
    if decision.recommended_refund_brl > 0 and decision.case_status != "action_required":
        decision.case_status = "action_required"
        fixes.append("REFUND_REQUIRES_ACTION_STATUS")

    party_types = {party["party_type"] for party in decision.responsible_parties}
    seller_fault = decision.primary_issue in {"late_delivery_seller", "unavailable_order_paid"}
    if seller_fault and ("logistics_provider" in party_types or "seller" not in party_types):
        decision.responsible_parties = [
            {"party_type": "seller", "party_id": sid} for sid in facts.seller_ids
        ] or [{"party_type": "seller", "party_id": None}]
        fixes.append("SELLER_FAULT_PARTIES_ALIGNED")
    if decision.primary_issue == "late_delivery_logistics" and "seller" in party_types:
        decision.responsible_parties = [{"party_type": "logistics_provider", "party_id": None}]
        fixes.append("LOGISTICS_FAULT_PARTIES_ALIGNED")

    decision.resolution_actions = list(dict.fromkeys(decision.resolution_actions))[:8]
    if not decision.resolution_actions:
        decision.resolution_actions = ["document_no_action"]
    decision.responsible_parties = decision.responsible_parties[:5]
    decision.ranked_causes = [
        {"cause_code": cause["cause_code"], "rank": rank}
        for rank, cause in enumerate(decision.ranked_causes[:5], 1)
    ]

    if fixes:
        decision.confidence = round(max(0.05, decision.confidence - 0.05 * len(fixes)), 2)
    return fixes
