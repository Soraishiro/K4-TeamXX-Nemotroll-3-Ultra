from __future__ import annotations

import asyncio
import logging
from typing import Any

from .mcp_gateway import EvidenceGateway, ToolExecutionError
from .policy_engine import Decision, Facts, decide, detect_findings, extract_facts, verify
from .trace import TraceWriter

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "day09-l3a-output-v2"
CURRENCY = "BRL"
CALL_TIMEOUT_SECONDS = 30.0

# Evidence that supports a conclusion about each issue/claim topic. Only these refs are cited,
# so e.g. a late-delivery conclusion does not cite payment evidence.
RELEVANT_TOOLS_BY_TOPIC: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": (
        "get_order", "get_order_payments", "get_payment_timeline", "get_policy",
    ),
    "unavailable_order_paid": (
        "get_order", "get_order_items", "get_sellers", "get_order_payments",
        "get_payment_timeline", "get_policy",
    ),
    "late_delivery_seller": (
        "get_order", "get_order_items", "get_shipment_summary", "get_sellers", "get_policy",
    ),
    "late_delivery_logistics": (
        "get_order", "get_order_items", "get_shipment_summary", "get_policy",
    ),
    "valid_split_payment": (
        "get_order", "get_order_items", "get_order_payments", "get_payment_timeline",
        "get_policy",
    ),
    "payment_mismatch": (
        "get_order", "get_order_payments", "get_payment_timeline", "get_policy",
    ),
    "duplicate_charge": (
        "get_order", "get_order_items", "get_order_payments", "get_payment_timeline",
        "get_policy",
    ),
    "refund_pending": ("get_order", "get_order_payments", "get_refund_timeline", "get_policy"),
    "refund_failed": ("get_order", "get_order_payments", "get_refund_timeline", "get_policy"),
    "unsupported_claim": (
        "get_order", "get_shipment_summary", "get_order_payments", "get_policy",
    ),
    "requested_full_refund": ("get_order", "get_order_payments", "get_policy"),
}


def _select_refs(refs_by_tool: dict[str, str], topics: list[str], fallback: list[str]) -> list[str]:
    """Pick refs from tools relevant to the given topics.
    Falls back to every ref when no topic is known."""
    tools = [tool for topic in topics for tool in RELEVANT_TOOLS_BY_TOPIC.get(topic, ())]
    if not tools:
        return fallback
    return list(dict.fromkeys(refs_by_tool[tool] for tool in tools if tool in refs_by_tool))


async def _safe_call_tool(
    gateway: EvidenceGateway,
    tool_name: str,
    *,
    case_id: str,
    retries: int = 2,
    backoff_seconds: float = 0.5,
    timeout_seconds: float = CALL_TIMEOUT_SECONDS,
    **arguments: str,
) -> dict[str, Any] | None:
    """Execute an MCP tool call with exponential backoff retry and error handling.
    Only transient failures (timeouts, transport errors) are retried: tool errors and
    invalid payloads are deterministic and every retry counts against the audited call budget.
    Returns the parsed evidence envelope or None if tool call failed."""
    for attempt in range(retries + 1):
        try:
            return await asyncio.wait_for(
                gateway.call(tool_name, case_id=case_id, **arguments), timeout_seconds
            )
        except (ToolExecutionError, ValueError) as exc:
            logger.warning("MCP tool %s failed for case %s: %s", tool_name, case_id, exc)
            return None
        except Exception as exc:
            if attempt == retries:
                logger.warning(
                    "MCP tool %s failed for case %s after %d attempts: %s",
                    tool_name,
                    case_id,
                    retries + 1,
                    exc,
                )
                return None
            await asyncio.sleep(backoff_seconds * (2**attempt))
    return None


async def _consume_tool(
    gateway: EvidenceGateway,
    trace: TraceWriter,
    actor: str,
    findings: dict[str, Any],
    tool_name: str,
    *,
    case_id: str,
    **arguments: str,
) -> Any:
    """Call a tool, record its server-issued evidence_ref per tool and emit tool_result_consumed.
    Returns the evidence data, or None if the call failed."""
    evidence = await _safe_call_tool(gateway, tool_name, case_id=case_id, **arguments)
    if not evidence:
        return None
    ref = evidence.get("evidence_ref")
    if ref:
        findings["evidence_refs"].append(ref)
        findings["evidence_by_tool"][tool_name] = ref
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[ref],
        )
    findings.setdefault("warnings", []).extend(evidence.get("warnings", []))
    return evidence.get("data")


def _rows(data: Any, key: str) -> list[dict[str, Any]]:
    """Normalize evidence data that is either a list of rows or a dict wrapping one."""
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get(key, [])
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


class OrderAgent:
    """Specialist Agent responsible for Order, Items, Sellers, and Product domain evidence."""

    ACTOR_NAME = "order_agent"
    PERMITTED_TOOLS = ("get_order", "get_order_items", "get_sellers", "get_product_context")

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(self, case_id: str, claimed_order_id: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "order_ids": [claimed_order_id] if claimed_order_id else [],
            "item_ids": [],
            "seller_ids": [],
            "order_data": None,
            "items_data": None,
            "sellers_data": None,
            "product_data": None,
            "evidence_refs": [],
            "evidence_by_tool": {},
            "status": "completed",
        }
        if not claimed_order_id:
            return result

        async def consume(tool_name: str) -> Any:
            return await _consume_tool(
                self.gateway, self.trace, self.ACTOR_NAME, result, tool_name,
                case_id=case_id, order_id=claimed_order_id,
            )

        result["order_data"] = await consume("get_order")

        result["items_data"] = await consume("get_order_items")
        for item in _rows(result["items_data"], "items"):
            if item.get("order_item_id"):
                result["item_ids"].append(str(item["order_item_id"]))
            if item.get("seller_id"):
                result["seller_ids"].append(str(item["seller_id"]))

        result["sellers_data"] = await consume("get_sellers")
        for seller in _rows(result["sellers_data"], "sellers"):
            if seller.get("seller_id"):
                result["seller_ids"].append(str(seller["seller_id"]))

        result["product_data"] = await consume("get_product_context")

        result["item_ids"] = list(dict.fromkeys(result["item_ids"]))
        result["seller_ids"] = list(dict.fromkeys(result["seller_ids"]))
        return result


class PaymentAgent:
    """Specialist Agent responsible for Payment and Refund domain evidence."""

    ACTOR_NAME = "payment_agent"
    PERMITTED_TOOLS = ("get_order_payments", "get_payment_timeline", "get_refund_timeline")

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(self, case_id: str, claimed_order_id: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "payment_references": [],
            "payments_data": None,
            "payment_timeline_data": None,
            "refund_data": None,
            "evidence_refs": [],
            "evidence_by_tool": {},
            "status": "completed",
        }
        if not claimed_order_id:
            return result

        async def consume(tool_name: str) -> Any:
            return await _consume_tool(
                self.gateway, self.trace, self.ACTOR_NAME, result, tool_name,
                case_id=case_id, order_id=claimed_order_id,
            )

        result["payments_data"] = await consume("get_order_payments")
        for payment in _rows(result["payments_data"], "payments"):
            if payment.get("payment_sequential") is not None:
                result["payment_references"].append(
                    f"{claimed_order_id}:{payment['payment_sequential']}"
                )

        result["payment_timeline_data"] = await consume("get_payment_timeline")
        # The server returns a tool error for orders without refund records; data stays None.
        result["refund_data"] = await consume("get_refund_timeline")

        result["payment_references"] = list(dict.fromkeys(result["payment_references"]))
        return result


class ShipmentAgent:
    """Specialist Agent responsible for Delivery and Shipment domain evidence."""

    ACTOR_NAME = "shipment_agent"
    PERMITTED_TOOLS = ("get_shipment_summary",)

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def investigate(self, case_id: str, claimed_order_id: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "shipment_ids": [],
            "shipment_data": None,
            "evidence_refs": [],
            "evidence_by_tool": {},
            "status": "completed",
        }
        if not claimed_order_id:
            return result

        data = await _consume_tool(
            self.gateway, self.trace, self.ACTOR_NAME, result, "get_shipment_summary",
            case_id=case_id, order_id=claimed_order_id,
        )
        result["shipment_data"] = data
        shipments = [data] if isinstance(data, dict) else _rows(data, "shipments")
        for shipment in shipments:
            sid = shipment.get("shipment_id") or shipment.get("shipping_id")
            if sid:
                result["shipment_ids"].append(str(sid))

        result["shipment_ids"] = list(dict.fromkeys(result["shipment_ids"]))
        return result


class PolicyAgent:
    """Specialist Agent evaluating policies, claims, primary issues, root cause, and financial resolution."""

    ACTOR_NAME = "policy_agent"
    PERMITTED_TOOLS = ("get_policy",)

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.gateway = gateway
        self.trace = trace

    async def evaluate(
        self,
        case: dict[str, Any],
        order_findings: dict[str, Any],
        payment_findings: dict[str, Any],
        shipment_findings: dict[str, Any],
    ) -> dict[str, Any]:
        case_id = case["case_id"]
        policy_version = case.get("policy_version", "EC_POLICY_V1")
        customer_request = case.get("customer_request", {})
        claims = customer_request.get("claims", [])

        # 1. Fetch policy
        policy_findings: dict[str, Any] = {"evidence_refs": [], "evidence_by_tool": {}}
        policy_data = await _consume_tool(
            self.gateway, self.trace, self.ACTOR_NAME, policy_findings, "get_policy",
            case_id=case_id, policy_version=policy_version,
        )

        # Aggregate evidence refs
        all_findings = (order_findings, payment_findings, shipment_findings, policy_findings)
        refs_by_tool: dict[str, str] = {}
        for findings in all_findings:
            refs_by_tool.update(findings.get("evidence_by_tool", {}))
        all_evidence_refs = list(
            dict.fromkeys(ref for f in all_findings for ref in f.get("evidence_refs", []))
        )

        # 2. Decide from evidence
        facts = extract_facts(
            customer_request.get("claimed_order_id"),
            order_findings, payment_findings, shipment_findings,
            opened_at=case.get("opened_at"),
        )
        facts.warnings.extend(policy_findings.get("warnings", []))
        provisional = detect_findings(facts)
        likely_issue = provisional[0].issue if provisional else "unsupported_claim"
        decision = decide(
            facts, claims, _evidence_completeness(likely_issue, refs_by_tool), policy_data
        )

        # 3. Cite what supports the decision and each claim under review, not every ref gathered.
        claim_topics = [str(claim.get("topic", "")) for claim in claims]
        case_refs = _select_refs(
            refs_by_tool, [decision.primary_issue, *claim_topics], all_evidence_refs
        )
        claim_assessments: list[dict[str, Any]] = []
        for claim in claims:
            cid = str(claim.get("claim_id", "claim-unknown"))
            verdict, c_conf = decision.claim_verdicts.get(cid, ("insufficient_evidence", 0.4))
            claim_refs = _select_refs(refs_by_tool, [str(claim.get("topic", ""))], case_refs)
            claim_assessments.append({
                "claim_id": cid,
                "verdict": verdict,
                "confidence": c_conf,
                "evidence_refs": [] if verdict == "insufficient_evidence" else claim_refs[:30],
            })

        self.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor=self.ACTOR_NAME,
            decision_code=f"POLICY_{decision.primary_issue.upper()}",
            evidence_refs=case_refs[:20],
            attributes={
                "case_status": decision.case_status,
                "recommended_refund_brl": decision.recommended_refund_brl,
                "secondary_issues": ",".join(decision.secondary_issues) or None,
                "data_conflicts": len(decision.data_conflicts),
            },
        )

        return {
            "decision": decision,
            "facts": facts,
            "claim_assessments": claim_assessments,
            "evidence_refs": case_refs,
        }


def _evidence_completeness(issue: str, refs_by_tool: dict[str, str]) -> float:
    """Share of the evidence an issue needs that was actually obtained. A missing refund
    timeline only counts for refund issues: the server has no refund record otherwise."""
    needed = [
        tool for tool in RELEVANT_TOOLS_BY_TOPIC.get(issue, ("get_order", "get_policy"))
        if tool != "get_refund_timeline" or issue.startswith("refund_")
    ]
    return sum(tool in refs_by_tool for tool in needed) / len(needed) if needed else 1.0


class VerifierAgent:
    """Specialist Agent verifying schema compliance and domain invariants before finalization."""

    ACTOR_NAME = "verifier_agent"

    def __init__(self, trace: TraceWriter) -> None:
        self.trace = trace

    def verify_and_finalize(
        self,
        case_id: str,
        policy_decision: dict[str, Any],
        order_findings: dict[str, Any],
        payment_findings: dict[str, Any],
        shipment_findings: dict[str, Any],
    ) -> dict[str, Any]:
        decision: Decision = policy_decision["decision"]
        facts: Facts = policy_decision["facts"]
        fixes = verify(decision, facts)

        evidence_refs = policy_decision.get("evidence_refs", [])[:30]
        cited = set(evidence_refs)
        claim_assessments = [
            {**claim, "evidence_refs": [ref for ref in claim["evidence_refs"] if ref in cited]}
            for claim in policy_decision.get("claim_assessments", [])[:5]
        ]

        output: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case_id,
            "assessment": {
                "primary_issue": decision.primary_issue,
                "case_status": decision.case_status,
                "confidence": decision.confidence,
            },
            "affected_entities": {
                "order_ids": list(dict.fromkeys(order_findings.get("order_ids", [])))[:20],
                # In-window records only: rows from outside the case window are not affected.
                "item_ids": facts.item_ids[:20],
                "seller_ids": facts.seller_ids[:20],
                "payment_references": facts.payment_references[:20],
                "shipment_ids": list(dict.fromkeys(shipment_findings.get("shipment_ids", [])))[:20],
            },
            "claim_assessments": claim_assessments,
            "root_cause_analysis": {
                "ranked_causes": decision.ranked_causes,
                "responsible_parties": decision.responsible_parties,
            },
            "evidence_refs": evidence_refs,
            "data_conflicts": decision.data_conflicts[:5],
            "financial_resolution": {
                "currency": CURRENCY,
                "recommended_refund_brl": decision.recommended_refund_brl,
                "refund_lines": decision.refund_lines,
            },
            "resolution_actions": decision.resolution_actions,
        }

        self.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=self.ACTOR_NAME,
            decision_code=(
                "VERIFIED_WITH_CORRECTIONS" if fixes else "VERIFIED_INVARIANTS_AND_SCHEMA"
            ),
            evidence_refs=evidence_refs[:20],
            attributes={
                "corrections": ",".join(fixes)[:200] or None,
                "confidence": decision.confidence,
            },
        )
        return output


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the Day09 L3A Multi-Agent Architecture workflow for a single case.
    
    Flow:
    1. Coordinator assigns tasks to Order, Payment, and Shipment agents.
    2. Specialist agents query domain MCP tools and emit tool_result_consumed.
    3. Specialists hand off findings to Policy Agent.
    4. Policy Agent evaluates claims, policy rules, root cause, and financial resolution.
    5. Policy Agent hands off to Verifier Agent.
    6. Verifier Agent validates invariants and constructs schema-compliant output.
    """
    case_id = case["case_id"]
    customer_request = case.get("customer_request", {})
    claimed_order_id = customer_request.get("claimed_order_id")

    # Step 1: Coordinator assigns tasks
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order_agent",
        attributes={"task": "investigate_order_and_items"},
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment_agent",
        attributes={"task": "investigate_payments_and_refunds"},
    )
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment_agent",
        attributes={"task": "investigate_shipment_timeline"},
    )

    # Step 2: Specialists investigate concurrently
    order_agent = OrderAgent(gateway, trace)
    payment_agent = PaymentAgent(gateway, trace)
    shipment_agent = ShipmentAgent(gateway, trace)

    order_task = order_agent.investigate(case_id, claimed_order_id)
    payment_task = payment_agent.investigate(case_id, claimed_order_id)
    shipment_task = shipment_agent.investigate(case_id, claimed_order_id)

    order_findings, payment_findings, shipment_findings = await asyncio.gather(
        order_task, payment_task, shipment_task
    )

    # Step 3: Handoff from Specialists to Policy Agent
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order_agent",
        target="policy_agent",
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="payment_agent",
        target="policy_agent",
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="shipment_agent",
        target="policy_agent",
    )

    # Step 4: Policy Agent evaluation
    policy_agent = PolicyAgent(gateway, trace)
    policy_decision = await policy_agent.evaluate(
        case, order_findings, payment_findings, shipment_findings
    )

    # Step 5: Handoff to Verifier Agent
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy_agent",
        target="verifier_agent",
    )

    # Step 6: Verifier Agent verification and final output
    verifier = VerifierAgent(trace)
    return verifier.verify_and_finalize(
        case_id, policy_decision, order_findings, payment_findings, shipment_findings
    )
