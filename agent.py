from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE = ROOT_DIR / ".env.agent.secret"
DEFAULT_TIMEOUT_SECONDS = 45
SYSTEM_PROMPT = (
    "You are a concise assistant. Answer the user's question directly and factually. "
    "Return plain text only."
)


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs from a dotenv-like file into os.environ."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ.setdefault(key, value)



def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    raise RuntimeError(f"Missing required environment variable: {name}")



def normalize_api_base(api_base: str) -> str:
    return api_base.rstrip("/")



def extract_text_content(message_content: Any) -> str:
    if isinstance(message_content, str):
        return message_content.strip()

    if isinstance(message_content, list):
        text_parts: list[str] = []
        for item in message_content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_value = item.get("text")
                if isinstance(text_value, str):
                    text_parts.append(text_value)
        return "\n".join(part.strip() for part in text_parts if part.strip())

    return str(message_content).strip()



def call_llm(question: str) -> str:
    load_env_file(ENV_FILE)

    api_key = require_env("LLM_API_KEY")
    api_base = normalize_api_base(require_env("LLM_API_BASE"))
    model = require_env("LLM_MODEL")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "temperature": 0,
    }

    request = urllib.request.Request(
        url=f"{api_base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM request failed with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM request failed: {exc}") from exc

    try:
        parsed = json.loads(raw_body)
        content = parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unexpected LLM response: {raw_body}") from exc

    answer = extract_text_content(content)
    if not answer:
        raise RuntimeError("LLM returned an empty answer")
    return answer



def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print(
            "Usage: uv run agent.py \"Your question\"",
            file=sys.stderr,
        )
        return 1

    question = argv[1].strip()

    try:
        answer = call_llm(question)
    except Exception as exc:  # pragma: no cover - exercised through subprocess tests
        print(f"agent.py error: {exc}", file=sys.stderr)
        return 1

    result = {"answer": answer, "tool_calls": []}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
