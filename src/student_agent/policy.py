"""Evidence-driven arbitration. Public scoring weights are not business policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .specialists import SpecialistResult, rows

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def money(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("Missing or invalid monetary value")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid monetary value") from exc
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(CENT):
        raise ValueError("Money must be finite non-negative whole cents")
    return amount.quantize(CENT)


def timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Ambiguous timestamp without timezone")
    return parsed


@dataclass
class EvidenceView:
    results: list[SpecialistResult]
    used: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def get(self, domain: str, *keys: str) -> list[dict[str, Any]]:
        found = []
        for result in self.results:
            for ref, envelope in result.envelopes.items():
                if envelope["domain"] == domain:
                    if ref not in self.used:
                        self.used.append(ref)
                    found.extend(rows(envelope["data"], *keys))
        return found

    def tool(self, names: tuple[str, ...], *keys: str) -> list[dict[str, Any]]:
        for name in names:
            found = []
            for result in self.results:
                for data in result.findings.get(name, []):
                    found.extend(rows(data, *keys))
            if found or any(name in r.findings for r in self.results):
                return found
        return []

    def conflict(
        self,
        field_name: str,
        sources: list[str],
        *,
        selected_source: str | None = None,
        resolution_code: str = "manual_review_required",
    ) -> None:
        if len(self.conflicts) < 5:
            self.conflicts.append(
                {
                    "field": field_name,
                    "sources": list(dict.fromkeys(sources))[:5],
                    "selected_source": selected_source,
                    "resolution_code": resolution_code,
                }
            )


def _unique_records(
    records: list[dict[str, Any]], key: str, view: EvidenceView
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        identity = str(record.get(key, f"row:{index}"))
        if identity in unique and unique[identity] != record:
            view.conflict(key, [f"{key}:{identity}:first"[:80], f"{key}:{identity}:other"[:80]])
            raise ValueError("Conflicting records share an identifier")
        unique[identity] = record
    return list(unique.values())


def _entities(view: EvidenceView) -> dict[str, list[str]]:
    mapping = {
        "order_ids": ("order_id",),
        "item_ids": ("order_item_id", "item_id"),
        "seller_ids": ("seller_id",),
        "payment_references": (
            "payment_reference",
            "payment_id",
            "transaction_id",
            "payment_sequential",
        ),
        "shipment_ids": ("shipment_id",),
    }
    values: dict[str, set[str]] = {key: set() for key in mapping}

    def visit(data: Any) -> None:
        if isinstance(data, dict):
            for target, names in mapping.items():
                for name in names:
                    if data.get(name) is not None:
                        values[target].add(str(data[name]))
            for value in data.values():
                visit(value)
        elif isinstance(data, list):
            for value in data:
                visit(value)

    for result in view.results:
        for envelope in result.envelopes.values():
            if envelope["domain"] != "policy":
                visit(envelope["data"])
    if any(len(value) > 20 for value in values.values()):
        raise ValueError("Entity count exceeds output contract")
    return {key: sorted(value) for key, value in values.items()}


def _window(
    records: list[dict[str, Any]],
    date_key: str,
    start: datetime | None,
    end: datetime | None,
) -> list[dict[str, Any]]:
    kept = []
    for record in records:
        date = timestamp(record.get(date_key))
        if date is None:
            raise ValueError("Undated record cannot be assigned to a timeline")
        if (start is None or date >= start) and (end is None or date <= end):
            kept.append(record)
    return kept


def _classify(
    case: dict[str, Any],
    view: EvidenceView,
) -> tuple[str, Decimal, list[dict[str, Any]], list[str]]:
    orders = view.get("order", "orders")
    items = view.get("item", "items")
    view.get("payment", "payments")
    view.get("refund", "refunds")
    shipments = view.get("shipment", "shipments")
    if len(orders) != 1:
        raise ValueError("Missing or ambiguous order")
    order = orders[0]
    purchase = timestamp(order.get("order_purchase_timestamp"))
    opened = timestamp(case.get("opened_at"))
    estimated = timestamp(order.get("order_estimated_delivery_date"))
    delivered = timestamp(order.get("order_delivered_customer_date"))
    if purchase and opened and purchase > opened:
        view.conflict("order_purchase_timestamp", ["order", "case_opened_at"])
        raise ValueError("Order begins after the dispute")
    payments = view.tool(("get_order_payments", "get_transaction"), "payments", "transactions")
    timeline = view.tool(("get_payment_timeline", "get_charge_details"), "events", "charges")
    captures = []
    if timeline:
        scoped = _window(timeline, "event_at", purchase, opened)
        if len(scoped) != len(timeline):
            view.conflict(
                "payment_event_scope",
                ["get_payment_timeline", "get_order"],
                selected_source="get_payment_timeline",
                resolution_code="filter_to_order_case_window",
            )
        timeline = scoped
        captures = [
            p
            for p in timeline
            if p.get("event_type") == "captured" and p.get("status") == "confirmed"
        ]
        payment_total = sum((money(p["amount_brl"]) for p in captures), ZERO)
    elif payments:
        payment_total = sum((money(p["payment_value"]) for p in payments), ZERO)
    else:
        raise ValueError("Missing payment records")
    refunds = view.tool(("get_refund_timeline", "get_refund_status"), "events", "refunds")
    if refunds and any("event_at" in row for row in refunds):
        scoped_refunds = _window(refunds, "event_at", purchase, opened)
        if len(scoped_refunds) != len(refunds):
            view.conflict(
                "refund_event_scope",
                ["get_refund_timeline", "get_order"],
                selected_source="get_refund_timeline",
                resolution_code="filter_to_order_case_window",
            )
        refunds = scoped_refunds
    completed = sum(
        (
            money(r.get("refund_amount", r.get("amount_brl")))
            for r in refunds
            if r.get("status") in {"completed", "succeeded", "refunded"}
        ),
        ZERO,
    )
    remaining = max(ZERO, payment_total - completed)
    if any(r.get("status") == "failed" for r in refunds):
        failed = sum(
            (
                money(r.get("refund_amount", r.get("amount_brl")))
                for r in refunds
                if r.get("status") == "failed"
            ),
            ZERO,
        )
        return "refund_failed", min(remaining, failed), [], ["payment_provider"]
    if any(r.get("status") in {"pending", "processing"} for r in refunds):
        return "refund_pending", ZERO, [], ["payment_provider"]
    status = order.get("order_status")
    if status in {"canceled", "unavailable"} and remaining > 0:
        return f"{status}_order_paid", remaining, [], []
    if purchase and estimated and any("shipping_limit_date" in i for i in items):
        scoped_items = _window(items, "shipping_limit_date", purchase, estimated)
        if len(scoped_items) != len(items):
            view.conflict(
                "item_shipping_timeline",
                ["get_order_items", "get_order"],
                selected_source="get_order_items",
                resolution_code="filter_to_order_delivery_window",
            )
        items = scoped_items
    items = _unique_records(items, "order_item_id", view)
    if not items:
        raise ValueError("Missing item records")
    expected = sum((money(i["price"]) + money(i["freight_value"]) for i in items), ZERO)
    if estimated and (delivered or opened) and (delivered or opened) > estimated:
        if not shipments:
            raise ValueError("Missing shipment evidence for late-delivery attribution")
        handoff = timestamp(order.get("order_delivered_carrier_date"))
        if handoff is None:
            raise ValueError("Missing carrier handoff timestamp")
        late_items = [
            item
            for item in items
            if timestamp(item.get("shipping_limit_date")) is not None
            and handoff > timestamp(item["shipping_limit_date"])
        ]
        actors = {
            event.get("actor")
            for shipment in shipments
            for event in shipment.get("events", [])
            if event.get("event_type") == "delivered_late" and event.get("status") == "confirmed"
        }
        if late_items and actors <= {"seller"}:
            return "late_delivery_seller", min(expected, remaining), late_items, ["seller"]
        if not late_items and actors <= {"logistics_provider", "logistics", "carrier"}:
            return (
                "late_delivery_logistics",
                min(expected, remaining),
                items,
                ["logistics_provider"],
            )
        raise ValueError("Carrier events conflict with dispatch timestamps")
    duplicates = [
        p
        for p in timeline
        if p.get("is_duplicate") is True or p.get("event_type") == "duplicate_charge"
    ]
    if duplicates:
        duplicate_total = sum((money(p["amount_brl"]) for p in duplicates), ZERO)
        return "duplicate_charge", min(remaining, duplicate_total), [], ["payment_provider"]
    mismatches = [
        p
        for p in timeline
        if p.get("event_type") == "reconciliation_mismatch" and p.get("status") == "open"
    ]
    if mismatches:
        correction = sum((money(p["amount_brl"]) for p in mismatches), ZERO)
        return "payment_mismatch", min(remaining, correction), [], ["payment_provider"]
    if payment_total != expected:
        return (
            "payment_mismatch",
            max(ZERO, payment_total - expected - completed),
            [],
            ["payment_provider"],
        )
    if delivered is None or estimated is None:
        raise ValueError("Incomplete delivery timeline")
    split_count = len(captures) if timeline else len(payments)
    issue = "valid_split_payment" if split_count > 1 else "unsupported_claim"
    return issue, ZERO, [], ["customer"]


def evaluate_policy(
    case: dict[str, Any],
    results: list[SpecialistResult],
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    view = EvidenceView(results)
    entities = _entities(view)
    issue, amount, lines, parties, actions = "insufficient_evidence", ZERO, [], [], []
    status, confidence = "needs_investigation", 0.4
    try:
        policies = view.get("policy")
        if len(policies) != 1 or policies[0].get("policy_version") != case["policy_version"]:
            raise ValueError("Missing or mismatched business policy")
        issue, eligible, affected, expected_parties = _classify(case, view)
        rule = policies[0]["rules"][issue]
        status = rule["case_status"]
        actions = [rule["recommended_action"]]
        parties = [dict(party) for party in rule["responsible_parties"]]
        for party in parties:
            if party["party_type"] == "seller":
                view.get("seller", "sellers")
                sellers = sorted({str(item["seller_id"]) for item in affected})
                sellers = sellers or entities["seller_ids"]
                if len(sellers) != 1:
                    raise ValueError("Cannot attribute fault to one seller")
                if party["party_id"] != sellers[0]:
                    view.conflict(
                        "responsible_parties",
                        ["get_policy", "get_order_items"],
                        selected_source="get_order_items",
                        resolution_code="bind_policy_role_to_order_seller",
                    )
                party["party_id"] = sellers[0]
        if expected_parties and any(p["party_type"] not in expected_parties for p in parties):
            view.conflict("responsible_parties", ["policy", "transaction_or_shipment"])
            raise ValueError("Policy responsibility conflicts with observed fault")
        if issue.startswith("late_delivery"):
            if rule["recommended_action"] != "refund_freight":
                raise ValueError("Unsupported delivery remedy")
            budget = eligible
            if "refund_brl" in rule:
                budget = min(budget, money(rule["refund_brl"]))
            for item in sorted(affected, key=lambda item: str(item["order_item_id"])):
                line_amount = min(budget, money(item["freight_value"]))
                if line_amount:
                    lines.append(
                        {
                            "reason_code": "refund_freight",
                            "amount_brl": float(line_amount),
                            "entity_id": str(item["order_item_id"]),
                        }
                    )
                    budget -= line_amount
            amount = sum((money(line["amount_brl"]) for line in lines), ZERO)
            if amount > eligible:
                raise ValueError("Compensation exceeds order value")
        else:
            amount = eligible
            if "refund_brl" in rule:
                amount = min(amount, money(rule["refund_brl"]))
            if amount:
                lines = [
                    {
                        "reason_code": rule["recommended_action"],
                        "amount_brl": float(amount),
                        "entity_id": case["customer_request"]["claimed_order_id"],
                    }
                ]
        confidence = 0.96 if all(r.status == "completed" for r in results) else 0.65
        if any(conflict["selected_source"] is None for conflict in view.conflicts):
            confidence = min(confidence, 0.6)
        elif view.conflicts:
            confidence = min(confidence, 0.85)
        refund_known = any(
            envelope["domain"] == "refund"
            for result in results
            for envelope in result.envelopes.values()
        )
        if amount and not refund_known:
            # A policy-backed recommendation is not an execution authorization.
            # Retain the quoted entitlement with an explicit prerequisite; without
            # an authoritative monetary rule, abstain from specifying an amount.
            if "refund_brl" in rule and money(rule["refund_brl"]) >= amount:
                actions.insert(0, "verify_refund_history")
            else:
                amount, lines = ZERO, []
                actions, status = ["verify_refund_history"], "needs_investigation"
            confidence = min(confidence, 0.65)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        issue, amount, lines, parties = "insufficient_evidence", ZERO, [], []
        actions, status, confidence = ["request_manual_review"], "needs_investigation", 0.4
    refs = list(dict.fromkeys(view.used))
    if len(refs) > 30:
        raise ValueError("Evidence references exceed output contract")
    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case["case_id"],
        "assessment": {"primary_issue": issue, "case_status": status, "confidence": confidence},
        "affected_entities": entities,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": refs,
        "data_conflicts": view.conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(amount),
            "refund_lines": lines,
        },
        "resolution_actions": actions,
    }
    assessments = []
    for claim in case["customer_request"].get("claims", [])[:5]:
        if issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif claim.get("topic") == "requested_full_refund":
            verdict = (
                "supported"
                if issue in {"canceled_order_paid", "unavailable_order_paid"}
                else ("partially_supported" if amount > 0 else "unsupported")
            )
        else:
            verdict = "supported" if claim.get("topic") == issue else "unsupported"
        assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )
    output["claim_assessments"] = assessments
    bindings = {action: refs for action in actions if refs}
    for line in lines:
        bindings[f"refund:{line['reason_code']}"] = refs
    return output, bindings
