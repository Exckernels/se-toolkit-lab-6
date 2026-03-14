from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_PATH = PROJECT_ROOT / "agent.py"


def test_agent_outputs_required_json_fields() -> None:
    completed = subprocess.run(
        [sys.executable, str(AGENT_PATH), "What does REST stand for?"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)

    assert "answer" in payload
    assert isinstance(payload["answer"], str)
    assert payload["answer"].strip()

    assert "tool_calls" in payload
    assert isinstance(payload["tool_calls"], list)
