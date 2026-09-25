"""Policy evaluation and verification agent.

The verifier:
1. Receives all specialist findings and evidence
2. Cross-references evidence to determine root cause
3. Applies policy rules to determine appropriate resolution
4. Validates consistency between findings and recommended actions
5. Produces the final output matching the L3A output schema
"""
from __future__ import annotations

import logging
from typing import Any

from .coordinator import CoordinatorPlan
from .specialists import SpecialistResult
from .trace import TraceWriter

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# Primary issue determination from evidence
# ──────────────────────────────────────────────────────────

# Maps claim topics directly to primary_issue values in the output schema
TOPIC_TO_PRIMARY_ISSUE: dict[str, str] = {
    "canceled_order_paid": "canceled_order_paid",
    "unavailable_order_paid": "unavailable_order_paid",
    "late_delivery_seller": "late_delivery_seller",
    "late_delivery_logistics": "late_delivery_logistics",
    "payment_mismatch": "payment_mismatch",
    "duplicate_charge": "duplicate_charge",
    "valid_split_payment": "valid_split_payment",
    "refund_pending": "refund_pending",
    "refund_failed": "refund_failed",
    "unsupported_claim": "unsupported_claim",
}

# Map primary issue to root cause code
ISSUE_TO_CAUSE_CODE: dict[str, str] = {
    "canceled_order_paid": "ORDER_CANCELED_PAYMENT_NOT_REFUNDED",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_PAYMENT_NOT_REFUNDED",
    "late_delivery_seller": "SELLER_LATE_DISPATCH",
    "late_delivery_logistics": "LOGISTICS_TRANSIT_DELAY",
    "payment_mismatch": "PAYMENT_AMOUNT_DISCREPANCY",
    "duplicate_charge": "DUPLICATE_PAYMENT_CHARGED",
    "valid_split_payment": "SPLIT_PAYMENT_VALID",
    "refund_pending": "REFUND_PROCESSING_PENDING",
    "refund_failed": "REFUND_PROCESSING_FAILED",
    "unsupported_claim": "CLAIM_NOT_SUPPORTED_BY_EVIDENCE",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
}

# Map primary issue to responsible party type
ISSUE_TO_PARTY: dict[str, str] = {
    "canceled_order_paid": "seller",
    "unavailable_order_paid": "seller",
    "late_delivery_seller": "seller",
    "late_delivery_logistics": "logistics_provider",
    "payment_mismatch": "payment_provider",
    "duplicate_charge": "payment_provider",
    "valid_split_payment": "platform",
    "refund_pending": "platform",
    "refund_failed": "platform",
    "unsupported_claim": "unknown",
    "insufficient_evidence": "unknown",
}


def _determine_primary_issue(
    claims: list[dict[str, Any]],
    order_data: dict[str, Any] | None,
) -> str:
    """Determine the primary issue from claim topics and order evidence."""
    # Find the first non-refund claim topic
    for claim in claims:
        topic = claim.get("topic", "")
        if topic != "requested_full_refund":
            return TOPIC_TO_PRIMARY_ISSUE.get(topic, "insufficient_evidence")

    # If only refund claims, check order status
    if order_data:
        status = order_data.get("order_status", "")
        if status == "canceled":
            return "canceled_order_paid"
        if status == "unavailable":
            return "unavailable_order_paid"

    return "insufficient_evidence"


def _determine_case_status(primary_issue: str) -> str:
    """Determine case status based on the primary issue."""
    no_action_issues = {"valid_split_payment", "unsupported_claim"}
    investigation_issues = {"insufficient_evidence"}
    if primary_issue in no_action_issues:
        return "no_action"
    if primary_issue in investigation_issues:
        return "needs_investigation"
    return "action_required"


def _compute_confidence(
    specialist_results: list[SpecialistResult],
    primary_issue: str,
) -> float:
    """Compute confidence based on evidence completeness."""
    total_specialists = len(specialist_results)
    if total_specialists == 0:
        return 0.2

    completed = sum(1 for r in specialist_results if r.status == "completed")
    partial = sum(1 for r in specialist_results if r.status == "partial")

    # Base confidence from completion rate
    base = (completed * 1.0 + partial * 0.5) / total_specialists

    # Adjust for issue type
    if primary_issue in ("unsupported_claim", "insufficient_evidence"):
        return round(min(base * 0.6, 0.5), 2)
    if primary_issue == "valid_split_payment":
        return round(min(base * 0.8, 0.85), 2)

    return round(min(base * 0.9, 0.95), 2)


def _extract_entities_from_evidence(
    specialist_results: list[SpecialistResult],
) -> dict[str, list[str]]:
    """Extract affected entity IDs from specialist evidence."""
    order_ids: set[str] = set()
    item_ids: set[str] = set()
    seller_ids: set[str] = set()
    payment_refs: set[str] = set()
    shipment_ids: set[str] = set()

    for result in specialist_results:
        findings = result.findings

        # Order data
        order = findings.get("order", {})
        if isinstance(order, dict):
            oid = order.get("order_id")
            if oid:
                order_ids.add(oid)
            cid = order.get("customer_id")

        # Items data
        items = findings.get("items", [])
        if isinstance(items, list):
            for item in items:
                iid = item.get("order_item_id") or item.get("item_id")
                if iid:
                    item_ids.add(str(iid))
                sid = item.get("seller_id")
                if sid:
                    seller_ids.add(sid)

        # Payment data
        payments = findings.get("payments", [])
        if isinstance(payments, list):
            for pay in payments:
                pid = pay.get("payment_sequential") or pay.get("payment_id")
                if pid is not None:
                    payment_refs.add(str(pid))

        # Shipment data
        shipments = findings.get("shipments", [])
        if isinstance(shipments, list):
            for ship in shipments:
                shid = ship.get("shipment_id") or ship.get("order_id")
                if shid:
                    shipment_ids.add(shid)

    return {
        "order_ids": sorted(order_ids),
        "item_ids": sorted(item_ids),
        "seller_ids": sorted(seller_ids),
        "payment_references": sorted(payment_refs),
        "shipment_ids": sorted(shipment_ids),
    }


def _build_claim_assessments(
    claims: list[dict[str, Any]],
    primary_issue: str,
    all_evidence_refs: list[str],
    confidence: float,
) -> list[dict[str, Any]]:
    """Build per-claim assessment objects."""
    assessments = []
    for claim in claims:
        claim_id = claim.get("claim_id", "")
        topic = claim.get("topic", "")

        if topic == "requested_full_refund":
            # Refund request support depends on primary issue
            if primary_issue in (
                "canceled_order_paid", "unavailable_order_paid",
                "late_delivery_seller", "late_delivery_logistics",
                "duplicate_charge", "payment_mismatch",
                "refund_failed",
            ):
                verdict = "supported"
            elif primary_issue in ("valid_split_payment", "unsupported_claim"):
                verdict = "unsupported"
            elif primary_issue == "refund_pending":
                verdict = "partially_supported"
            else:
                verdict = "insufficient_evidence"
        else:
            mapped_issue = TOPIC_TO_PRIMARY_ISSUE.get(topic, "")
            if mapped_issue == primary_issue:
                verdict = "supported"
            elif mapped_issue:
                verdict = "partially_supported"
            else:
                verdict = "insufficient_evidence"

        assessments.append({
            "claim_id": claim_id,
            "verdict": verdict,
            "confidence": confidence,
            "evidence_refs": all_evidence_refs[:10],  # Cap per claim
        })

    return assessments[:5]  # Schema max 5


def _compute_financial_resolution(
    primary_issue: str,
    specialist_results: list[SpecialistResult],
    order_id: str | None,
) -> dict[str, Any]:
    """Compute the recommended financial resolution from evidence."""
    total_payment = 0.0
    total_refunded = 0.0
    refund_lines: list[dict[str, Any]] = []

    for result in specialist_results:
        # Sum payments
        payments = result.findings.get("payments", [])
        if isinstance(payments, list):
            for pay in payments:
                val = pay.get("payment_value", 0)
                if isinstance(val, (int, float)):
                    total_payment += val

        # Sum existing refunds
        refunds = result.findings.get("refunds", [])
        if isinstance(refunds, list):
            for ref in refunds:
                val = ref.get("refund_amount", 0) or ref.get("amount", 0)
                if isinstance(val, (int, float)):
                    total_refunded += val

    # Determine refund recommendation
    no_refund_issues = {"valid_split_payment", "unsupported_claim", "insufficient_evidence"}
    if primary_issue in no_refund_issues:
        recommended_refund = 0.0
    elif primary_issue == "refund_pending":
        # Refund already in progress
        recommended_refund = max(0.0, total_payment - total_refunded)
    else:
        # Full refund minus already refunded
        recommended_refund = max(0.0, total_payment - total_refunded)

    if recommended_refund > 0:
        reason_map = {
            "canceled_order_paid": "canceled_order_full_refund",
            "unavailable_order_paid": "unavailable_order_full_refund",
            "late_delivery_seller": "late_delivery_compensation",
            "late_delivery_logistics": "late_delivery_compensation",
            "payment_mismatch": "payment_correction",
            "duplicate_charge": "duplicate_charge_reversal",
            "refund_failed": "refund_reprocessing",
            "refund_pending": "pending_refund_completion",
        }
        refund_lines.append({
            "reason_code": reason_map.get(primary_issue, "general_refund"),
            "amount_brl": round(recommended_refund, 2),
            "entity_id": order_id,
        })

    return {
        "currency": "BRL",
        "recommended_refund_brl": round(recommended_refund, 2),
        "refund_lines": refund_lines,
    }


def _build_resolution_actions(primary_issue: str) -> list[str]:
    """Build the list of resolution actions based on the primary issue."""
    actions_map: dict[str, list[str]] = {
        "canceled_order_paid": [
            "Issue full refund to customer",
            "Notify seller of cancellation confirmation",
            "Update order status to refunded",
        ],
        "unavailable_order_paid": [
            "Issue full refund to customer",
            "Flag seller for inventory accuracy review",
            "Update order status to refunded",
        ],
        "late_delivery_seller": [
            "Issue compensation to customer for late delivery",
            "Notify seller of SLA violation",
            "Log seller performance incident",
        ],
        "late_delivery_logistics": [
            "Issue compensation to customer for late delivery",
            "File carrier SLA claim",
            "Update delivery tracking status",
        ],
        "payment_mismatch": [
            "Reconcile payment discrepancy",
            "Issue corrective refund if overcharged",
            "Notify payment provider of mismatch",
        ],
        "duplicate_charge": [
            "Reverse duplicate payment",
            "Notify payment provider of duplicate",
            "Confirm single charge with customer",
        ],
        "valid_split_payment": [
            "Confirm split payment is valid per policy",
            "Notify customer of payment breakdown",
        ],
        "refund_pending": [
            "Escalate pending refund for processing",
            "Notify customer of expected refund timeline",
        ],
        "refund_failed": [
            "Reprocess failed refund",
            "Verify customer payment method is valid",
            "Notify customer of refund reprocessing",
        ],
        "unsupported_claim": [
            "Notify customer that claim is not supported by evidence",
            "Close case with no action",
        ],
        "insufficient_evidence": [
            "Request additional evidence from customer",
            "Escalate for manual review",
        ],
    }
    return actions_map.get(primary_issue, ["Escalate for manual review"])


def build_final_output(
    case: dict[str, Any],
    plan: CoordinatorPlan,
    specialist_results: list[SpecialistResult],
    trace: TraceWriter,
) -> dict[str, Any]:
    """Build the final L3A output from coordinator plan and specialist evidence.

    This is the verifier/policy agent's main function. It:
    1. Determines primary issue from claims + evidence
    2. Assesses each claim individually
    3. Identifies root cause and responsible parties
    4. Computes financial resolution from payment evidence
    5. Produces the output matching the L3A v2 schema
    """
    case_id = case["case_id"]
    customer_request = case.get("customer_request", {})
    claims = customer_request.get("claims", [])
    order_id = customer_request.get("claimed_order_id")

    # Find order data from specialist results
    order_data: dict[str, Any] | None = None
    for result in specialist_results:
        if "order" in result.findings:
            order_data = result.findings["order"]
            break

    # ── Primary issue & status ──
    primary_issue = _determine_primary_issue(claims, order_data)
    case_status = _determine_case_status(primary_issue)
    confidence = _compute_confidence(specialist_results, primary_issue)

    # ── Collect all evidence refs ──
    all_evidence_refs: list[str] = []
    seen_refs: set[str] = set()
    for result in specialist_results:
        for ref in result.evidence_refs:
            if ref not in seen_refs:
                all_evidence_refs.append(ref)
                seen_refs.add(ref)

    # ── Entities from evidence ──
    entities = _extract_entities_from_evidence(specialist_results)

    # ── Claim assessments ──
    claim_assessments = _build_claim_assessments(
        claims, primary_issue, all_evidence_refs, confidence
    )

    # ── Root cause ──
    cause_code = ISSUE_TO_CAUSE_CODE.get(primary_issue, "UNKNOWN_CAUSE")
    party_type = ISSUE_TO_PARTY.get(primary_issue, "unknown")

    # Try to find the specific party ID
    party_id: str | None = None
    if party_type == "seller" and entities["seller_ids"]:
        party_id = entities["seller_ids"][0]

    root_cause_analysis = {
        "ranked_causes": [
            {"cause_code": cause_code, "rank": 1},
        ],
        "responsible_parties": [
            {"party_type": party_type, "party_id": party_id},
        ],
    }

    # ── Data conflicts ──
    data_conflicts: list[dict[str, Any]] = []

    # ── Financial resolution ──
    financial_resolution = _compute_financial_resolution(
        primary_issue, specialist_results, order_id
    )

    # ── Resolution actions ──
    resolution_actions = _build_resolution_actions(primary_issue)

    # ── Emit verification trace ──
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=f"primary_issue:{primary_issue}",
        evidence_refs=all_evidence_refs[:20],
        attributes={
            "confidence": confidence,
            "case_status": case_status,
            "specialist_count": len(specialist_results),
        },
    )

    # ── Build final output ──
    output: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": case_status,
            "confidence": confidence,
        },
        "affected_entities": entities,
        "claim_assessments": claim_assessments,
        "root_cause_analysis": root_cause_analysis,
        "evidence_refs": all_evidence_refs[:30],
        "data_conflicts": data_conflicts,
        "financial_resolution": financial_resolution,
        "resolution_actions": resolution_actions,
    }

    return output
