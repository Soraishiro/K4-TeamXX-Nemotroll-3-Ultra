"""Master Coordinator / Supervisor Agent.

Analyzes incoming dispute cases, classifies the primary complaint domain,
and produces a deterministic handoff plan for domain-specific specialist agents.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ──────────────────────────────────────────────────────────
# Topic → intent-category mapping (from actual case data)
# ──────────────────────────────────────────────────────────
TOPIC_TO_INTENT: dict[str, str] = {
    "canceled_order_paid": "canceled_unrefunded",
    "unavailable_order_paid": "canceled_unrefunded",
    "late_delivery_seller": "delivery_delay",
    "late_delivery_logistics": "delivery_delay",
    "payment_mismatch": "payment_mismatch",
    "duplicate_charge": "payment_mismatch",
    "valid_split_payment": "payment_mismatch",
    "refund_pending": "canceled_unrefunded",
    "refund_failed": "canceled_unrefunded",
    "unsupported_claim": "multi_issue",
    "requested_full_refund": "canceled_unrefunded",  # secondary, usually paired
}

# ──────────────────────────────────────────────────────────
# Intent → required specialist agents (topologically sorted)
# ──────────────────────────────────────────────────────────
INTENT_TO_SPECIALISTS: dict[str, list[str]] = {
    "delivery_delay": ["order_specialist", "shipment_specialist"],
    "canceled_unrefunded": ["order_specialist", "payment_specialist"],
    "payment_mismatch": ["order_specialist", "payment_specialist"],
    "wrong_damaged_item": ["order_specialist", "shipment_specialist"],
    "multi_issue": ["order_specialist", "shipment_specialist", "payment_specialist"],
}

# ──────────────────────────────────────────────────────────
# Specialist → MCP tools the specialist is allowed to call
# ──────────────────────────────────────────────────────────
SPECIALIST_TOOLS: dict[str, list[str]] = {
    "order_specialist": ["get_order", "get_order_items", "get_seller"],
    "payment_specialist": ["get_order_payments", "get_refunds"],
    "shipment_specialist": ["get_shipments", "get_order_items"],
}

# ──────────────────────────────────────────────────────────
# Task instruction templates per specialist per intent
# ──────────────────────────────────────────────────────────
TASK_INSTRUCTIONS: dict[str, dict[str, str]] = {
    "delivery_delay": {
        "order_specialist": (
            "Retrieve order details and item list for order_id={order_id}. "
            "Verify order status, purchase timestamp, and estimated delivery date. "
            "Identify seller_id(s) for downstream responsibility analysis."
        ),
        "shipment_specialist": (
            "Retrieve shipment records for order_id={order_id}. "
            "Compare carrier_delivered_at vs estimated_delivery_date. "
            "Determine if delay is attributable to seller dispatch or logistics transit."
        ),
    },
    "canceled_unrefunded": {
        "order_specialist": (
            "Retrieve order details and item list for order_id={order_id}. "
            "Confirm order_status is 'canceled' or 'unavailable'. "
            "Record purchase timestamp and all item_ids/seller_ids."
        ),
        "payment_specialist": (
            "Retrieve payment records and refund history for order_id={order_id}. "
            "Verify total payment amount, payment method, and whether any refund "
            "has been issued. Determine refund gap if applicable."
        ),
    },
    "payment_mismatch": {
        "order_specialist": (
            "Retrieve order details and item list for order_id={order_id}. "
            "Compute expected order total from item prices + freight. "
            "Record all item_ids and seller_ids."
        ),
        "payment_specialist": (
            "Retrieve all payment records for order_id={order_id}. "
            "Sum actual charges and compare against expected total. "
            "Check for duplicate payment_sequential entries or split-payment discrepancies."
        ),
    },
    "wrong_damaged_item": {
        "order_specialist": (
            "Retrieve order details and item list for order_id={order_id}. "
            "Record product categories, quantities, and seller_ids."
        ),
        "shipment_specialist": (
            "Retrieve shipment records for order_id={order_id}. "
            "Verify proof of delivery and item count against order manifest."
        ),
    },
    "multi_issue": {
        "order_specialist": (
            "Retrieve order details and item list for order_id={order_id}. "
            "Record order status, timestamps, item_ids, and seller_ids."
        ),
        "shipment_specialist": (
            "Retrieve shipment records for order_id={order_id}. "
            "Verify delivery status, timestamps, and carrier tracking."
        ),
        "payment_specialist": (
            "Retrieve payment records and refund history for order_id={order_id}. "
            "Verify payment amounts, methods, and refund status."
        ),
    },
}


@dataclass
class HandoffStep:
    """A single step in the handoff sequence."""
    step: int
    agent: str
    task_instruction: str
    priority: str  # high | medium | low


@dataclass
class CoordinatorPlan:
    """The coordinator's analysis and routing plan for a dispute case."""
    case_id: str
    intent_category: str
    primary_entities: dict[str, str | None]
    handoff_sequence: list[HandoffStep]
    preliminary_notes: str


def _classify_intent(claims: list[dict[str, Any]]) -> str:
    """Determine the dominant intent category from the claim topics.

    Uses a priority system: if multiple topics map to different intents,
    we use the first non-secondary topic's intent. If topics span multiple
    domains, we escalate to multi_issue.
    """
    intents_seen: set[str] = set()
    primary_intent: str | None = None

    for claim in claims:
        topic = claim.get("topic", "")
        if topic == "requested_full_refund":
            # This is always a secondary/supporting claim; skip for classification
            continue
        intent = TOPIC_TO_INTENT.get(topic, "multi_issue")
        intents_seen.add(intent)
        if primary_intent is None:
            primary_intent = intent

    if not intents_seen:
        # Only had requested_full_refund → default to canceled_unrefunded
        return "canceled_unrefunded"
    if len(intents_seen) > 1:
        return "multi_issue"
    return primary_intent or "multi_issue"


def _extract_entities(case: dict[str, Any]) -> dict[str, str | None]:
    """Extract primary entity identifiers from case data."""
    customer_request = case.get("customer_request", {})
    return {
        "order_id": customer_request.get("claimed_order_id"),
        "customer_id": case.get("customer_id"),
        "tracking_id": case.get("tracking_id"),
        "payment_id": case.get("payment_id"),
    }


def _build_handoff_sequence(
    intent_category: str,
    order_id: str | None,
) -> list[HandoffStep]:
    """Build the topologically-sorted handoff sequence for the given intent."""
    specialists = INTENT_TO_SPECIALISTS.get(intent_category, [])
    instructions = TASK_INSTRUCTIONS.get(intent_category, {})

    steps: list[HandoffStep] = []
    for idx, agent in enumerate(specialists, start=1):
        template = instructions.get(agent, f"Investigate {intent_category} for order_id={order_id}")
        task_instruction = template.format(order_id=order_id or "UNKNOWN")

        # First specialist is always high priority; others medium
        priority = "high" if idx == 1 else "medium"
        steps.append(HandoffStep(
            step=idx,
            agent=agent,
            task_instruction=task_instruction,
            priority=priority,
        ))
    return steps


def _generate_preliminary_notes(
    case: dict[str, Any],
    intent_category: str,
    claims: list[dict[str, Any]],
) -> str:
    """Generate preliminary notes summarizing the coordinator's analysis."""
    topics = [c.get("topic", "unknown") for c in claims]
    claim_ids = [c.get("claim_id", "?") for c in claims]
    order_id = case.get("customer_request", {}).get("claimed_order_id", "N/A")
    policy = case.get("policy_version", "N/A")

    return (
        f"Case classified as '{intent_category}'. "
        f"Claims: {claim_ids} with topics {topics}. "
        f"Target order: {order_id}. Policy version: {policy}. "
        f"Customer message language: {case.get('customer_request', {}).get('language', 'unknown')}."
    )


def plan_case(case: dict[str, Any]) -> CoordinatorPlan:
    """Analyze a dispute case and produce the coordinator's routing plan.

    This is the main entry point for the coordinator agent. It:
    1. Extracts entity identifiers (preserving them exactly)
    2. Classifies the primary complaint domain from claim topics
    3. Builds a deterministic handoff sequence to specialist agents

    The coordinator does NOT query evidence directly. It only orchestrates.
    """
    case_id = case["case_id"]
    customer_request = case.get("customer_request", {})
    claims = customer_request.get("claims", [])

    intent_category = _classify_intent(claims)
    entities = _extract_entities(case)
    order_id = entities.get("order_id")
    handoff_sequence = _build_handoff_sequence(intent_category, order_id)
    preliminary_notes = _generate_preliminary_notes(case, intent_category, claims)

    return CoordinatorPlan(
        case_id=case_id,
        intent_category=intent_category,
        primary_entities=entities,
        handoff_sequence=handoff_sequence,
        preliminary_notes=preliminary_notes,
    )


def plan_to_json(plan: CoordinatorPlan) -> dict[str, Any]:
    """Serialize the coordinator plan to the output JSON schema."""
    return {
        "case_id": plan.case_id,
        "intent_category": plan.intent_category,
        "primary_entities": plan.primary_entities,
        "handoff_sequence": [
            {
                "step": step.step,
                "agent": step.agent,
                "task_instruction": step.task_instruction,
                "priority": step.priority,
            }
            for step in plan.handoff_sequence
        ],
        "preliminary_notes": plan.preliminary_notes,
    }
