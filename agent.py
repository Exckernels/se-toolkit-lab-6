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
MAX_TOOL_CALLS = 10
SYSTEM_PROMPT = (
    "You are a documentation agent for this repository. "
    "Answer questions using the project wiki and repository files. "
    "Start by using list_files to discover relevant files, especially under wiki/. "
    "Then use read_file to inspect the most relevant file. "
    "When you know the answer, respond with a JSON object containing exactly two string fields: "
    '"answer" and "source". '
    "The source must be the best supporting reference, preferably a wiki path with a section anchor such as "
    'wiki/git.md#merge-conflict. '
    "Do not invent files or anchors. "
    "Use tools for repository facts instead of guessing."
)


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and directories for a relative path in the repository. "
                "Use this first to discover wiki files before reading them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path from the repository root, for example 'wiki'.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the repository using a relative path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path from the repository root, for example 'wiki/git.md'.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]


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

    if message_content is None:
        return ""

    return str(message_content).strip()


def parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None

    candidates = [stripped]
    if "```" in stripped:
        for block in stripped.split("```"):
            block = block.strip()
            if not block:
                continue
            if block.startswith("json"):
                block = block[4:].strip()
            candidates.append(block)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    return None


def resolve_repo_path(path: str) -> Path:
    normalized = (path or ".").strip()
    candidate = (ROOT_DIR / normalized).resolve()
    try:
        candidate.relative_to(ROOT_DIR)
    except ValueError as exc:
        raise ValueError(f"Path escapes repository root: {path}") from exc
    return candidate


def read_file(path: str) -> str:
    try:
        target = resolve_repo_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"

    if not target.exists():
        return f"ERROR: File does not exist: {path}"
    if not target.is_file():
        return f"ERROR: Path is not a file: {path}"

    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"ERROR: Could not read file {path}: {exc}"


def list_files(path: str) -> str:
    try:
        target = resolve_repo_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"

    if not target.exists():
        return f"ERROR: Directory does not exist: {path}"
    if not target.is_dir():
        return f"ERROR: Path is not a directory: {path}"

    try:
        entries = sorted(target.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        return f"ERROR: Could not list directory {path}: {exc}"

    lines = [f"{entry.name}/" if entry.is_dir() else entry.name for entry in entries]
    return "\n".join(lines)


TOOL_IMPLEMENTATIONS: dict[str, Any] = {
    "read_file": read_file,
    "list_files": list_files,
}


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    tool = TOOL_IMPLEMENTATIONS.get(name)
    if tool is None:
        return f"ERROR: Unknown tool: {name}"

    path = arguments.get("path")
    if not isinstance(path, str):
        return "ERROR: Tool argument 'path' must be a string"

    return tool(path)


def call_llm(messages: list[dict[str, Any]]) -> dict[str, Any]:
    load_env_file(ENV_FILE)

    api_key = require_env("LLM_API_KEY")
    api_base = normalize_api_base(require_env("LLM_API_BASE"))
    model = require_env("LLM_MODEL")

    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
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
        message = parsed["choices"][0]["message"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unexpected LLM response: {raw_body}") from exc

    if not isinstance(message, dict):
        raise RuntimeError(f"Unexpected LLM response: {raw_body}")
    return message


def parse_final_answer(content: str, fallback_source: str) -> tuple[str, str]:
    parsed = parse_json_object(content)
    if parsed is not None:
        answer = parsed.get("answer")
        source = parsed.get("source")
        if isinstance(answer, str) and answer.strip():
            final_answer = answer.strip()
        else:
            final_answer = content.strip()
        if isinstance(source, str) and source.strip():
            final_source = source.strip()
        else:
            final_source = fallback_source
        return final_answer, final_source

    answer = content.strip()
    source = fallback_source

    for line in content.splitlines():
        if line.lower().startswith("source:"):
            source_candidate = line.split(":", 1)[1].strip()
            if source_candidate:
                source = source_candidate
        if line.lower().startswith("answer:"):
            answer_candidate = line.split(":", 1)[1].strip()
            if answer_candidate:
                answer = answer_candidate

    return answer, source


def infer_fallback_source(tool_calls: list[dict[str, Any]]) -> str:
    for tool_call in reversed(tool_calls):
        if tool_call["tool"] == "read_file":
            path = tool_call["args"].get("path")
            if isinstance(path, str) and path.strip():
                return path.strip()
        if tool_call["tool"] == "list_files":
            path = tool_call["args"].get("path")
            if isinstance(path, str) and path.strip():
                return path.strip()
    return "unknown"


def answer_question(question: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    tool_calls_log: list[dict[str, Any]] = []
    last_answer = ""
    last_source = ""

    while True:
        message = call_llm(messages)
        content = extract_text_content(message.get("content"))
        current_tool_calls = message.get("tool_calls")
        if not isinstance(current_tool_calls, list):
            current_tool_calls = []

        fallback_source = infer_fallback_source(tool_calls_log)
        if content:
            parsed_answer, parsed_source = parse_final_answer(content, fallback_source)
            if parsed_answer:
                last_answer = parsed_answer
            if parsed_source:
                last_source = parsed_source

        if not current_tool_calls:
            final_answer = last_answer or "I could not determine the answer."
            final_source = last_source or fallback_source
            return {
                "answer": final_answer,
                "source": final_source,
                "tool_calls": tool_calls_log,
            }

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "tool_calls": current_tool_calls,
        }
        if content:
            assistant_message["content"] = content
        messages.append(assistant_message)

        limit_reached = False
        for tool_call in current_tool_calls:
            if len(tool_calls_log) >= MAX_TOOL_CALLS:
                limit_reached = True
                break

            function_payload = tool_call.get("function")
            if not isinstance(function_payload, dict):
                function_payload = {}

            name = function_payload.get("name")
            arguments_text = function_payload.get("arguments", "{}")
            if not isinstance(name, str) or not name.strip():
                name = "unknown"

            try:
                parsed_arguments = json.loads(arguments_text)
            except json.JSONDecodeError:
                parsed_arguments = {}

            if not isinstance(parsed_arguments, dict):
                parsed_arguments = {}

            result = execute_tool(name, parsed_arguments)
            tool_calls_log.append(
                {
                    "tool": name,
                    "args": parsed_arguments,
                    "result": result,
                }
            )

            tool_call_id = tool_call.get("id")
            if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                tool_call_id = f"tool_call_{len(tool_calls_log)}"

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result,
                }
            )

        if limit_reached:
            fallback_source = infer_fallback_source(tool_calls_log)
            final_answer = last_answer or "I could not finish before reaching the tool-call limit."
            final_source = last_source or fallback_source
            return {
                "answer": final_answer,
                "source": final_source,
                "tool_calls": tool_calls_log,
            }


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print(
            'Usage: uv run agent.py "Your question"',
            file=sys.stderr,
        )
        return 1

    question = argv[1].strip()

    try:
        result = answer_question(question)
    except Exception as exc:  # pragma: no cover - exercised through tests via main
        print(f"agent.py error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
