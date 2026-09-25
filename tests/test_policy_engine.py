from __future__ import annotations

from typing import Any

import pytest

from student_agent.policy_engine import Decision, decide, extract_facts, verify

ORDER_ID = "order_01"
PURCHASED = "2018-03-01T09:00:00-03:00"
OPENED = "2018-03-15T09:00:00-03:00"
NOISE_AT = "2018-07-01T10:00:00-03:00"  # after the case was opened: not part of this dispute


def _item(
    limit: str = "2018-03-04T09:00:00-03:00", price: str = "79.00", freight: str = "10.00"
) -> dict[str, Any]:
    return {
        "order_id": ORDER_ID,
        "order_item_id": "item-01",
        "seller_id": "seller-01",
        "shipping_limit_date": limit,
        "price": price,
        "freight_value": freight,
    }


def _payment(value: str, sequential: str = "1", kind: str = "credit_card") -> dict[str, Any]:
    return {"payment_sequential": sequential, "payment_type": kind, "payment_value": value}


def _event(
    at: str, amount: str, event_type: str = "captured", status: str = "confirmed"
) -> dict[str, Any]:
    return {"event_at": at, "event_type": event_type, "amount_brl": amount, "status": status}


def _run(
    *,
    status: str = "delivered",
    carrier: str | None = "2018-03-03T09:00:00-03:00",
    delivered: str | None = "2018-03-08T09:00:00-03:00",
    estimated: str = "2018-03-10T09:00:00-03:00",
    items: list[dict[str, Any]] | None = None,
    payments: list[dict[str, Any]] | None = None,
    payment_events: list[dict[str, Any]] | None = None,
    refund_events: list[dict[str, Any]] | None = None,
    topic: str = "placeholder",
    has_order: bool = True,
    policy: Any = None,
) -> tuple[Decision, list[str]]:
    order = {
        "order_id": ORDER_ID,
        "order_status": status,
        "order_purchase_timestamp": PURCHASED,
        "order_delivered_carrier_date": carrier,
        "order_delivered_customer_date": delivered,
        "order_estimated_delivery_date": estimated,
    }
    payments = payments if payments is not None else [_payment("89.00")]
    payment_events = (
        payment_events
        if payment_events is not None
        else [_event("2018-03-01T10:00:00-03:00", "89.00")]
    )
    facts = extract_facts(
        ORDER_ID,
        {"order_data": order if has_order else None, "items_data": items or [_item()]},
        {
            "payments_data": payments,
            "payment_timeline_data": {"payments": payments, "events": payment_events},
            "refund_data": {"events": refund_events} if refund_events else None,
        },
        {"shipment_data": {"order_status": status, "events": []}},
        opened_at=OPENED,
    )
    claims = [
        {"claim_id": "claim-a", "topic": topic},
        {"claim_id": "claim-b", "topic": "requested_full_refund"},
    ]
    decision = decide(facts, claims, evidence_completeness=1.0, policy_data=policy)
    fixes = verify(decision, facts)
    return decision, fixes


def _party_types(decision: Decision) -> set[str]:
    return {party["party_type"] for party in decision.responsible_parties}


def test_canceled_paid_order_refunds_what_was_captured_in_the_case_window() -> None:
    decision, fixes = _run(
        status="canceled",
        delivered=None,
        payments=[_payment("79.00"), _payment("18.00")],
        payment_events=[_event("2018-03-01T10:00:00-03:00", "79.00"), _event(NOISE_AT, "18.00")],
    )
    assert decision.primary_issue == "canceled_order_paid"
    assert decision.case_status == "action_required"
    assert decision.recommended_refund_brl == 79.0
    assert decision.resolution_actions == ["issue_refund"]
    assert _party_types(decision) == {"platform"}
    assert decision.claim_verdicts["claim-b"][0] == "supported"
    assert any(c["field"] == "payment_events" for c in decision.data_conflicts)
    assert fixes == []


def test_unavailable_order_blames_the_seller_with_id() -> None:
    decision, _ = _run(status="unavailable", delivered=None)
    assert decision.primary_issue == "unavailable_order_paid"
    assert decision.responsible_parties == [{"party_type": "seller", "party_id": "seller-01"}]
    assert decision.recommended_refund_brl == 89.0


def test_late_delivery_after_missed_shipping_limit_is_seller_fault() -> None:
    decision, _ = _run(
        carrier="2018-03-06T09:00:00-03:00",
        delivered="2018-03-12T09:00:00-03:00",
        items=[_item(freight="18.00"), _item(limit=NOISE_AT, freight="10.00")],
    )
    assert decision.primary_issue == "late_delivery_seller"
    assert _party_types(decision) == {"seller"}
    assert decision.recommended_refund_brl == 18.0
    assert decision.resolution_actions == ["refund_freight"]
    assert decision.claim_verdicts["claim-b"][0] == "partially_supported"


def test_late_delivery_with_on_time_handover_is_logistics_fault_capped_at_paid() -> None:
    decision, _ = _run(
        delivered="2018-03-12T09:00:00-03:00",
        items=[_item(freight="18.00")],
        payments=[_payment("16.00")],
        payment_events=[_event("2018-03-01T10:00:00-03:00", "16.00")],
    )
    assert decision.primary_issue == "late_delivery_logistics"
    assert _party_types(decision) == {"logistics_provider"}
    assert decision.recommended_refund_brl == 16.0


def test_equal_split_matching_order_total_needs_no_action() -> None:
    decision, _ = _run(
        payments=[_payment("44.50"), _payment("44.50", "2", "voucher")],
        payment_events=[
            _event("2018-03-01T10:00:00-03:00", "44.50"),
            _event("2018-03-01T11:00:00-03:00", "44.50"),
        ],
        refund_events=[_event("2018-01-10T09:00:00-03:00", "52.00", "refund_requested", "failed")],
    )
    assert decision.primary_issue == "valid_split_payment"
    assert decision.case_status == "no_action"
    assert decision.recommended_refund_brl == 0.0
    assert _party_types(decision) == {"customer"}
    assert any(c["field"] == "refund_events" for c in decision.data_conflicts)


def test_reconciliation_mismatch_event_refunds_the_mismatch() -> None:
    decision, _ = _run(
        payments=[_payment("35.00")],
        payment_events=[
            _event("2018-03-01T10:00:00-03:00", "35.00"),
            _event("2018-03-01T12:00:00-03:00", "35.00", "reconciliation_mismatch", "open"),
        ],
    )
    assert decision.primary_issue == "payment_mismatch"
    assert decision.recommended_refund_brl == 35.0
    assert decision.resolution_actions == ["reconcile_payment"]


def test_repeated_capture_exceeding_total_is_a_duplicate_charge() -> None:
    decision, _ = _run(
        payments=[_payment("64.00"), _payment("64.00", "2", "voucher")],
        payment_events=[
            _event("2018-03-01T10:00:00-03:00", "64.00"),
            _event("2018-03-01T11:00:00-03:00", "64.00"),
        ],
    )
    assert decision.primary_issue == "duplicate_charge"
    assert decision.recommended_refund_brl == 64.0
    assert decision.refund_lines[0]["entity_id"] == f"{ORDER_ID}:2"


@pytest.mark.parametrize(
    ("status", "issue", "case_status", "refund"),
    [
        ("pending", "refund_pending", "needs_investigation", 0.0),
        ("failed", "refund_failed", "action_required", 52.0),
    ],
)
def test_refund_timeline_status_drives_refund_issues(
    status: str, issue: str, case_status: str, refund: float
) -> None:
    decision, _ = _run(
        refund_events=[_event("2018-03-12T09:00:00-03:00", "52.00", "refund_requested", status)]
    )
    assert decision.primary_issue == issue
    assert decision.case_status == case_status
    assert decision.recommended_refund_brl == refund


def test_out_of_window_late_delivery_event_does_not_support_the_claim() -> None:
    decision, _ = _run(topic="unsupported_claim")
    assert decision.primary_issue == "unsupported_claim"
    assert decision.case_status == "no_action"
    assert decision.recommended_refund_brl == 0.0
    assert decision.claim_verdicts["claim-a"][0] == "unsupported"


def test_missing_order_evidence_is_insufficient() -> None:
    decision, _ = _run(has_order=False)
    assert decision.primary_issue == "insufficient_evidence"
    assert decision.case_status == "needs_investigation"
    assert decision.confidence <= 0.3


def test_served_policy_rules_override_defaults() -> None:
    policy = {
        "rules": {
            "canceled_order_paid": {
                "case_status": "action_required",
                "recommended_action": "issue_store_credit",
                "responsible_parties": [{"party_type": "seller", "party_id": "seller-x"}],
            }
        }
    }
    decision, _ = _run(status="canceled", delivered=None, policy=policy)
    assert decision.resolution_actions[-1] == "issue_store_credit"
    assert decision.responsible_parties == [{"party_type": "seller", "party_id": "seller-01"}]


def test_claim_topic_only_breaks_ties_between_supported_issues() -> None:
    late_and_mismatch = dict(
        delivered="2018-03-12T09:00:00-03:00",
        payments=[_payment("35.00")],
        payment_events=[
            _event("2018-03-01T10:00:00-03:00", "35.00"),
            _event("2018-03-01T12:00:00-03:00", "35.00", "reconciliation_mismatch", "open"),
        ],
    )
    default, _ = _run(**late_and_mismatch)
    assert default.primary_issue == "payment_mismatch"
    tie_broken, _ = _run(**late_and_mismatch, topic="late_delivery_logistics")
    assert tie_broken.primary_issue == "late_delivery_logistics"
    assert tie_broken.confidence > default.confidence


def test_confidence_is_capped() -> None:
    decision, _ = _run(status="canceled", delivered=None)
    assert decision.confidence <= 0.95


def test_verifier_repairs_inconsistent_decision() -> None:
    decision, _ = _run(carrier="2018-03-06T09:00:00-03:00", delivered="2018-03-12T09:00:00-03:00")
    decision.responsible_parties = [{"party_type": "logistics_provider", "party_id": None}]
    decision.recommended_refund_brl = 999.0
    decision.resolution_actions = ["document_no_action", "document_no_action"]
    before = decision.confidence
    facts = extract_facts(
        ORDER_ID,
        {"order_data": {"order_status": "delivered"}, "items_data": [_item()]},
        {"payments_data": [_payment("89.00")]},
        {"shipment_data": None},
    )

    fixes = verify(decision, facts)

    assert "SELLER_FAULT_PARTIES_ALIGNED" in fixes
    assert "REFUND_TOTAL_RECOMPUTED_FROM_LINES" in fixes
    assert "ADDED_REFUND_ACTION" in fixes
    assert _party_types(decision) == {"seller"}
    assert decision.recommended_refund_brl == 10.0
    assert decision.resolution_actions == ["issue_refund", "document_no_action"]
    assert decision.confidence < before


def test_capture_long_after_checkout_is_not_part_of_the_order() -> None:
    decision, _ = _run(
        status="canceled",
        delivered=None,
        payments=[_payment("79.00"), _payment("18.00")],
        payment_events=[
            _event("2018-03-01T10:00:00-03:00", "79.00"),
            _event("2018-03-10T10:00:00-03:00", "18.00"),  # inside the case window, not checkout
        ],
    )
    assert decision.primary_issue == "canceled_order_paid"
    assert decision.recommended_refund_brl == 79.0
    assert decision.secondary_issues == []


def test_identical_capture_rows_are_one_record_not_a_double_charge() -> None:
    decision, _ = _run(
        status="unavailable",
        delivered=None,
        payments=[_payment("89.00"), _payment("89.00")],
        payment_events=[
            _event("2018-03-01T10:00:00-03:00", "89.00"),
            _event("2018-03-01T10:00:00-03:00", "89.00"),
        ],
    )
    assert decision.primary_issue == "unavailable_order_paid"
    assert decision.recommended_refund_brl == 89.0
    assert "duplicate_charge" not in decision.secondary_issues
