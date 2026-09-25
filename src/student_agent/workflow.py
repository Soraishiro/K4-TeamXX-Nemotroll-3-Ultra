"""L3A Multi-Agent Workflow: Coordinator → Specialists → Verifier → Output.

The solve_case() function orchestrates the full dispute investigation:

1. Coordinator analyzes the case and plans the handoff sequence
2. Specialist agents execute their tasks in topological order
3. Specialists hand off findings with proper trace events
4. Verifier synthesizes evidence into the final output

No evidence is fabricated. All evidence_refs come from actual MCP responses.
"""
from __future__ import annotations

import logging
from typing import Any

from .coordinator import plan_case
from .mcp_gateway import EvidenceGateway
from .specialists import SpecialistResult, dispatch_specialist
from .trace import TraceWriter
from .verifier import build_final_output

logger = logging.getLogger(__name__)


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Implement the L3A coordinator and specialist-agent workflow.

    Flow:
        Input case → Coordinator (plan) → Specialist agents (evidence)
        → Handoffs (trace) → Verifier (output) → Final JSON
    """
    case_id = case["case_id"]
    customer_request = case.get("customer_request", {})
    order_id = customer_request.get("claimed_order_id")

    if not order_id:
        raise ValueError(f"Case {case_id} has no claimed_order_id")

    # ── Phase 1: Coordinator plans the investigation ──
    plan = plan_case(case)
    logger.info(
        "[%s] Coordinator classified as '%s', dispatching %d specialists",
        case_id, plan.intent_category, len(plan.handoff_sequence),
    )

    # ── Phase 2: Execute specialist agents in handoff order ──
    specialist_results: list[SpecialistResult] = []

    for step in plan.handoff_sequence:
        agent_name = step.agent
        logger.info("[%s] Step %d: dispatching %s", case_id, step.step, agent_name)

        result = await dispatch_specialist(
            agent_name=agent_name,
            case_id=case_id,
            order_id=order_id,
            gateway=gateway,
            trace=trace,
        )
        specialist_results.append(result)

        # ── Emit handoff trace between specialists ──
        if step.step < len(plan.handoff_sequence):
            next_agent = plan.handoff_sequence[step.step].agent
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=agent_name.replace("_", "-"),
                target=next_agent.replace("_", "-"),
                decision_code=f"handoff_step_{step.step}_to_{step.step + 1}",
                evidence_refs=result.evidence_refs[:10],
            )

    # ── Emit final handoff from last specialist to verifier ──
    if specialist_results:
        last_agent = plan.handoff_sequence[-1].agent
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=last_agent.replace("_", "-"),
            target="verifier",
            decision_code="handoff_to_verifier",
            evidence_refs=specialist_results[-1].evidence_refs[:10],
        )

    # ── Phase 3: Verifier builds final output ──
    output = build_final_output(case, plan, specialist_results, trace)

    # ── Emit policy decision trace ──
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="verifier",
        decision_code=f"resolution:{output['assessment']['primary_issue']}",
        attributes={
            "recommended_refund_brl": output["financial_resolution"]["recommended_refund_brl"],
            "case_status": output["assessment"]["case_status"],
        },
    )

    logger.info(
        "[%s] Resolved: issue=%s, status=%s, confidence=%.2f, refund=%.2f BRL",
        case_id,
        output["assessment"]["primary_issue"],
        output["assessment"]["case_status"],
        output["assessment"]["confidence"],
        output["financial_resolution"]["recommended_refund_brl"],
    )

    return output
