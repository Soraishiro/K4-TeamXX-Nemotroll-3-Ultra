"""Domain-specific specialist agents for the e-commerce dispute system.

Each specialist:
1. Receives task instructions from the coordinator
2. Calls the appropriate MCP tools to gather evidence
3. Returns structured findings and evidence references
4. Emits trace events for workflow observability

Specialists never fabricate evidence. If MCP returns an error,
the specialist reports the failure rather than guessing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)


@dataclass
class SpecialistResult:
    """Structured output from a specialist agent's investigation."""
    agent: str
    status: str  # "completed" | "partial" | "failed"
    findings: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────
# Order Specialist
# ──────────────────────────────────────────────────────────

async def run_order_specialist(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> SpecialistResult:
    """Retrieve order details, items, and seller information.

    Responsible for establishing the ground truth about order status,
    item composition, and seller identity.
    """
    result = SpecialistResult(agent="order-specialist")

    # ── Emit task_assigned trace ──
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-specialist",
        decision_code="investigate_order",
    )

    # ── Step 1: Get order details ──
    try:
        order_evidence = await gateway.call(
            "get_order", case_id=case_id, order_id=order_id
        )
        result.evidence_refs.append(order_evidence["evidence_ref"])
        result.findings["order"] = order_evidence["data"]

        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order-specialist",
            tool_name="get_order",
            evidence_refs=[order_evidence["evidence_ref"]],
        )
    except Exception as exc:
        logger.warning("get_order failed for %s: %s", order_id, exc)
        result.errors.append(f"get_order: {exc}")

    # ── Step 2: Get order items ──
    try:
        items_evidence = await gateway.call(
            "get_order_items", case_id=case_id, order_id=order_id
        )
        result.evidence_refs.append(items_evidence["evidence_ref"])
        result.findings["items"] = items_evidence["data"]

        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="order-specialist",
            tool_name="get_order_items",
            evidence_refs=[items_evidence["evidence_ref"]],
        )
    except Exception as exc:
        logger.warning("get_order_items failed for %s: %s", order_id, exc)
        result.errors.append(f"get_order_items: {exc}")

    # ── Step 3: Get seller info for each seller found in items ──
    sellers_seen: set[str] = set()
    items_data = result.findings.get("items", [])
    if isinstance(items_data, list):
        for item in items_data:
            sid = item.get("seller_id")
            if sid and sid not in sellers_seen:
                sellers_seen.add(sid)
                try:
                    seller_evidence = await gateway.call(
                        "get_seller", case_id=case_id, seller_id=sid
                    )
                    result.evidence_refs.append(seller_evidence["evidence_ref"])
                    result.findings.setdefault("sellers", {})[sid] = seller_evidence["data"]

                    trace.emit(
                        case_id=case_id,
                        event_type="tool_result_consumed",
                        actor="order-specialist",
                        tool_name="get_seller",
                        evidence_refs=[seller_evidence["evidence_ref"]],
                    )
                except Exception as exc:
                    logger.warning("get_seller failed for %s: %s", sid, exc)
                    result.errors.append(f"get_seller({sid}): {exc}")

    result.status = "completed" if not result.errors else ("partial" if result.findings else "failed")
    return result


# ──────────────────────────────────────────────────────────
# Payment Specialist
# ──────────────────────────────────────────────────────────

async def run_payment_specialist(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> SpecialistResult:
    """Retrieve payment records and refund history.

    Responsible for verifying payment amounts, detecting duplicate charges,
    and determining refund status.
    """
    result = SpecialistResult(agent="payment-specialist")

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment-specialist",
        decision_code="investigate_payment",
    )

    # ── Step 1: Get payments ──
    try:
        payment_evidence = await gateway.call(
            "get_order_payments", case_id=case_id, order_id=order_id
        )
        result.evidence_refs.append(payment_evidence["evidence_ref"])
        result.findings["payments"] = payment_evidence["data"]

        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="payment-specialist",
            tool_name="get_order_payments",
            evidence_refs=[payment_evidence["evidence_ref"]],
        )
    except Exception as exc:
        logger.warning("get_order_payments failed for %s: %s", order_id, exc)
        result.errors.append(f"get_order_payments: {exc}")

    # ── Step 2: Get refunds ──
    try:
        refund_evidence = await gateway.call(
            "get_refunds", case_id=case_id, order_id=order_id
        )
        result.evidence_refs.append(refund_evidence["evidence_ref"])
        result.findings["refunds"] = refund_evidence["data"]

        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="payment-specialist",
            tool_name="get_refunds",
            evidence_refs=[refund_evidence["evidence_ref"]],
        )
    except Exception as exc:
        # Refunds may not exist for all orders – that's OK
        logger.info("get_refunds returned nothing for %s: %s", order_id, exc)
        result.errors.append(f"get_refunds: {exc}")

    result.status = "completed" if not result.errors else ("partial" if result.findings else "failed")
    return result


# ──────────────────────────────────────────────────────────
# Shipment Specialist
# ──────────────────────────────────────────────────────────

async def run_shipment_specialist(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> SpecialistResult:
    """Retrieve shipment records and delivery status.

    Responsible for verifying delivery timelines, carrier information,
    and proof of delivery.
    """
    result = SpecialistResult(agent="shipment-specialist")

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment-specialist",
        decision_code="investigate_shipment",
    )

    # ── Step 1: Get shipments ──
    try:
        shipment_evidence = await gateway.call(
            "get_shipments", case_id=case_id, order_id=order_id
        )
        result.evidence_refs.append(shipment_evidence["evidence_ref"])
        result.findings["shipments"] = shipment_evidence["data"]

        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="shipment-specialist",
            tool_name="get_shipments",
            evidence_refs=[shipment_evidence["evidence_ref"]],
        )
    except Exception as exc:
        logger.warning("get_shipments failed for %s: %s", order_id, exc)
        result.errors.append(f"get_shipments: {exc}")

    result.status = "completed" if not result.errors else ("partial" if result.findings else "failed")
    return result


# ──────────────────────────────────────────────────────────
# Specialist dispatcher
# ──────────────────────────────────────────────────────────

SPECIALIST_RUNNERS = {
    "order_specialist": run_order_specialist,
    "payment_specialist": run_payment_specialist,
    "shipment_specialist": run_shipment_specialist,
}


async def dispatch_specialist(
    agent_name: str,
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> SpecialistResult:
    """Dispatch to the appropriate specialist agent by name."""
    runner = SPECIALIST_RUNNERS.get(agent_name)
    if runner is None:
        return SpecialistResult(
            agent=agent_name,
            status="failed",
            errors=[f"Unknown specialist: {agent_name}"],
        )
    return await runner(case_id, order_id, gateway, trace)
