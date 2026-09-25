from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from .cases import CaseSet, load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _show_tools(root: Path, describe: bool = False) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(
                json.dumps({"name": tool, "input_schema": gateway.tool_schemas[tool]})
                if describe
                else tool
            )


async def _inspect_case(root: Path, case_id: str, tool_names: list[str]) -> None:
    """Inspect real envelopes for a manifest case, without creating a decision."""
    settings = Settings.load(root)
    case = load_case_set(root).cases[case_id]
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        await gateway.list_tools()
        for name in tool_names:
            params = (
                {"policy_version": case["policy_version"]}
                if name == "get_policy"
                else {"order_id": case["customer_request"]["claimed_order_id"]}
            )
            try:
                evidence = await gateway.call(name, case_id=case_id, **params)
                print(json.dumps({"tool": name, "envelope": evidence}, ensure_ascii=False))
            except (RuntimeError, ValueError) as exc:
                print(json.dumps({"tool": name, "error": str(exc)}))


async def _run(root: Path, concurrency: int = 4) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    staging_parent = (root / "traces").resolve()
    staging_parent.mkdir(parents=True, exist_ok=True)
    if not staging_parent.is_relative_to(root.resolve()):
        raise ValueError("Run staging directory must stay inside the workspace")
    with tempfile.TemporaryDirectory(prefix=".run-", dir=staging_parent) as directory:
        staging = Path(directory).resolve()
        if staging.parent != staging_parent:
            raise ValueError("Invalid run staging path")
        await _collect(staging, concurrency, settings, case_set, contracts)
        validate_artifacts(staging, case_set, contracts)
        (root / "outputs").mkdir(parents=True, exist_ok=True)
        for case_id in case_set.case_ids:
            (staging / "outputs" / f"{case_id}.json").replace(root / "outputs" / f"{case_id}.json")
        for name in ("trace.jsonl", "evidence.json"):
            (staging / "traces" / name).replace(root / "traces" / name)


async def _collect(
    root: Path,
    concurrency: int,
    settings: Settings,
    case_set: CaseSet,
    contracts: Contracts,
) -> None:
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        trace = TraceWriter(trace_path, contracts)
        semaphore = asyncio.Semaphore(concurrency)

        async def run_one(case_id: str) -> None:
            async with semaphore:
                await solve_one(case_id)

        async def solve_one(case_id: str) -> None:
            case = case_set.cases[case_id]
            trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
            output = await solve_case(case, gateway, trace)
            contracts.validate_output(output, f"outputs/{case_id}.json")
            if output.get("case_id") != case_id:
                raise ValueError(f"solver returned a mismatched case_id for {case_id}")
            target = output_root / f"{case_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(target)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            print(
                f"{case_id}: {output['assessment']['primary_issue']} / "
                f"{output['assessment']['case_status']}",
                flush=True,
            )

        try:
            async with asyncio.TaskGroup() as tasks:
                for case_id in case_set.case_ids:
                    tasks.create_task(run_one(case_id))
        finally:
            # Raw envelopes remain local, outside the submission ZIP.
            (root / "traces" / "evidence.json").write_text(
                json.dumps(gateway.records, ensure_ascii=False, indent=2), encoding="utf-8"
            )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3A student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    discovery = commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    discovery.add_argument("--describe", action="store_true", help="include tool input schemas")
    inspect = commands.add_parser("inspect-case", help="read raw evidence for a manifest case")
    inspect.add_argument("case_id")
    inspect.add_argument("tools", nargs="+")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument("--concurrency", type=int, choices=range(1, 9), default=4)
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root, args.describe))
        elif args.command == "run":
            asyncio.run(_run(root, args.concurrency))
        elif args.command == "inspect-case":
            asyncio.run(_inspect_case(root, args.case_id, args.tools))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except ExceptionGroup as exc:
        cause: BaseException = exc
        while isinstance(cause, BaseExceptionGroup):
            cause = cause.exceptions[0]
        print(f"ERROR: run failed ({type(cause).__name__}): {cause}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
