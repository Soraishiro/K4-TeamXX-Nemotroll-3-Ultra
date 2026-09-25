from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from jsonschema import Draft202012Validator, ValidationError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams

from .contracts import Contracts


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self.tool_schemas: dict[str, dict[str, Any]] = {}
        self._owners: dict[str, str] = {}
        self.records: dict[str, dict[str, dict[str, Any]]] = {}

    async def list_tools(self) -> list[str]:
        if not self.tool_schemas:
            cursor = None
            while True:
                params = PaginatedRequestParams(cursor=cursor) if cursor else None
                response = await self._session.list_tools(params=params)
                for tool in response.tools:
                    self.tool_schemas[tool.name] = tool.input_schema
                cursor = response.next_cursor
                if not cursor:
                    break
        return sorted(self.tool_schemas)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        await self.list_tools()
        if tool_name not in self.tool_schemas:
            raise ValueError(f"Tool was not discovered: {tool_name}")
        try:
            Draft202012Validator(self.tool_schemas[tool_name]).validate(payload)
        except ValidationError as exc:
            raise ValueError(f"Invalid arguments for discovered tool {tool_name}") from exc
        # No automatic retries: a timed-out call may already have an MCP audit entry.
        async with asyncio.timeout(60):
            result = await self._session.call_tool(tool_name, arguments=payload)
        if result.is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        ref = evidence["evidence_ref"]
        if ref in self._owners and self._owners[ref] != case_id:
            raise ValueError("Gateway returned an evidence_ref owned by another case")
        previous = self.records.get(case_id, {}).get(ref)
        if previous is not None and previous != evidence:
            raise ValueError("Gateway changed an existing evidence envelope")

        def check_scope(data: Any) -> None:
            if isinstance(data, dict):
                if "case_id" in data and data["case_id"] != case_id:
                    raise ValueError("Gateway data has a mismatched case_id")
                if (
                    "order_id" in data
                    and "order_id" in arguments
                    and data["order_id"] != arguments["order_id"]
                ):
                    raise ValueError("Gateway data has a mismatched order_id")
                for value in data.values():
                    check_scope(value)
            elif isinstance(data, list):
                for value in data:
                    check_scope(value)

        check_scope(evidence["data"])
        self._owners[ref] = case_id
        self.records.setdefault(case_id, {})[ref] = evidence
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
