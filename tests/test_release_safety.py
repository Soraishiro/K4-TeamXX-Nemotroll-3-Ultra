import subprocess
from pathlib import Path


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    assert "case-set.json" not in tracked
    assert ".env" not in tracked
    assert not any(
        name.startswith(("inputs/", "outputs/", "traces/")) and not name.endswith(".gitkeep")
        for name in tracked
        if name
    )
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(set(Path(name).parts) & forbidden for name in tracked if name)


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
