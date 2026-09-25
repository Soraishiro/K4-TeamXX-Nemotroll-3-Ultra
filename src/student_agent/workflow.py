"""Sequential, bounded A2A workflow with evidence-linked policy and verification."""

from __future__ import annotations

from typing import Any

from .coordinator import plan_case
from .mcp_gateway import EvidenceGateway
from .policy import evaluate_policy
from .specialists import SpecialistResult, dispatch_specialist
from .trace import TraceWriter
from .verifier import verify_output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    order_id = case.get("customer_request", {}).get("claimed_order_id")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError(f"Case {case_id} has no claimed_order_id")
    plan = plan_case(case)
    results: list[SpecialistResult] = []
    for agent in plan.agents:
        if results:
            previous = results[-1]
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=previous.agent,
                target=agent.replace("_", "-"),
                decision_code="evidence_handoff",
                evidence_refs=previous.evidence_refs[:20],
                attributes={"retrieval_errors": len(previous.errors)},
            )
        result = await dispatch_specialist(
            agent,
            case_id,
            order_id,
            gateway,
            trace,
            context=results,
            policy_version=case.get("policy_version"),
        )
        results.append(result)
        if agent == "order_specialist" and "get_order" not in result.findings:
            raise RuntimeError(
                f"{case_id}: get_order returned no authoritative evidence; run aborted"
            )
    output, bindings = evaluate_policy(case, results)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-specialist",
        decision_code=output["assessment"]["primary_issue"],
        evidence_refs=output["evidence_refs"][:20],
    )
    for action, refs in bindings.items():
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-specialist",
            decision_code=action,
            evidence_refs=refs[:20],
        )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-specialist",
        target="verifier",
        decision_code="verify_proposed_resolution",
        evidence_refs=output["evidence_refs"][:20],
    )
    verify_output(output, case_id, results, trace.contracts)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="schema_and_consistency_passed",
        evidence_refs=output["evidence_refs"][:20],
        attributes={"confidence": output["assessment"]["confidence"]},
    )
    return output
