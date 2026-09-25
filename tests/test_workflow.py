from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import ToolExecutionError
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case

ROOT = Path(__file__).resolve().parents[1]

OLIST_DATA: dict[str, Any] = {
    "get_order": {
        "order_status": "canceled",
        "order_purchase_timestamp": "2017-12-20T09:00:00-03:00",
        "order_delivered_carrier_date": "2017-12-22T09:00:00-03:00",
        "order_delivered_customer_date": None,
        "order_estimated_delivery_date": "2017-12-30T09:00:00-03:00",
    },
    "get_order_items": [
        {
            "order_item_id": "item-01",
            "seller_id": "seller-01",
            "price": "100.00",
            "freight_value": "20.00",
            "shipping_limit_date": "2017-12-23T09:00:00-03:00",
        },
    ],
    "get_order_payments": [
        {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "120.00"},
    ],
    "get_sellers": [{"seller_id": "seller-01"}],
    "get_product_context": [{"order_item_id": "item-01", "category_name_english": "housewares"}],
    "get_payment_timeline": {
        "events": [
            {
                "event_at": "2017-12-20T10:00:00-03:00",
                "event_type": "captured",
                "amount_brl": "120.00",
                "status": "confirmed",
            }
        ]
    },
    "get_shipment_summary": {"order_status": "canceled", "events": []},
    "get_policy": {"policy_version": "EC_POLICY_V1"},
}


def _gateway(refs: dict[str, str]) -> AsyncMock:
    async def mock_call(tool_name: str, case_id: str, **kwargs: str) -> dict:
        if tool_name not in OLIST_DATA:
            raise ToolExecutionError(f"MCP tool {tool_name} failed: Error executing tool")
        refs[tool_name] = f"ev_{tool_name}_1234567890123456"
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": refs[tool_name],
            "result_hash": "sha256:" + "f" * 64,
            "domain": "order",
            "data": OLIST_DATA[tool_name],
        }

    gateway = AsyncMock()
    gateway.call.side_effect = mock_call
    return gateway


def _solve(tmp_path: Path, refs: dict[str, str]) -> tuple[dict, list[dict]]:
    contracts = Contracts(ROOT / "contracts" / "schemas")
    case = json.loads((ROOT / "inputs" / "L3A_CASE_001.json").read_text("utf-8"))
    trace_file = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_file, contracts)
    trace.emit(case_id=case["case_id"], event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, _gateway(refs), trace))
    trace.emit(case_id=case["case_id"], event_type="case_finalized", actor="coordinator")
    contracts.validate_output(output, "workflow output")
    events = [json.loads(line) for line in trace_file.read_text("utf-8").splitlines()]
    for event in events:
        contracts.validate_trace(event, "trace event")
    return output, events


def test_solve_case_matches_contracts(tmp_path: Path) -> None:
    output, events = _solve(tmp_path, {})

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 120.0
    assert output["resolution_actions"] == ["issue_refund"]
    lifecycle = [e["event_type"] for e in events]
    for required in (
        "case_received",
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "policy_decided",
        "verification_completed",
        "case_finalized",
    ):
        assert required in lifecycle
    assert lifecycle.index("policy_decided") < lifecycle.index("verification_completed")


def test_solve_case_cites_only_relevant_evidence(tmp_path: Path) -> None:
    refs: dict[str, str] = {}
    output, events = _solve(tmp_path, refs)

    assert set(refs) == set(OLIST_DATA)
    consumed = {e["tool_name"] for e in events if e["event_type"] == "tool_result_consumed"}
    assert consumed == set(refs)

    cited = set(output["evidence_refs"])
    relevant = {"get_order", "get_order_payments", "get_payment_timeline", "get_policy"}
    assert cited == {refs[tool] for tool in relevant}
    for claim in output["claim_assessments"]:
        assert set(claim["evidence_refs"]) <= cited
