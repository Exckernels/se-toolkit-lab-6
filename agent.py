from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


ROOT_DIR = Path(__file__).resolve().parent
ENV_FILES = [ROOT_DIR / ".env.agent.secret", ROOT_DIR / ".env.docker.secret"]
DEFAULT_TIMEOUT_SECONDS = 45
MAX_TOOL_CALLS = 10
DEFAULT_AGENT_API_BASE_URL = "http://localhost:42002"
SYSTEM_PROMPT = (
    "You are a repository and system agent for this project. "
    "The wiki can be outdated, so use the real system or source code when the question is about current backend behavior. "
    "Choose tools carefully: "
    "use list_files to discover directories or router modules, "
    "use read_file for wiki pages, source code, Docker files, and configuration, "
    "and use query_api for live API behavior, data counts, status codes, and endpoint errors. "
    "For bug-diagnosis questions, query the API first to observe the real error, then read the relevant source file and explain the cause. "
    "For framework questions, read the source code instead of guessing. "
    "For router-module questions, list backend/app/routers. "
    "When query_api returns both an authenticated result and an unauthenticated_request field, "
    "use the unauthenticated_request field only for questions that explicitly ask about missing authentication; otherwise use the top-level status_code and body. "
    "When you know the answer, respond with a JSON object containing an 'answer' string and, when helpful, a 'source' string. "
    "The source should be the best supporting file path or endpoint path, and it may be omitted for pure API answers. "
    "Do not invent files, anchors, endpoints, or values. "
    "Use tools instead of guessing."
)


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and directories for a relative path in the repository. "
                "Use this to discover wiki pages, backend modules, and router files before reading them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path from the repository root, for example 'wiki' or 'backend/app/routers'.",
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
            "description": (
                "Read a UTF-8 text file from the repository using a relative path. "
                "Use this for wiki pages, Python source code, docker-compose.yml, Dockerfile, and other config files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path from the repository root, for example 'wiki/git.md' or 'backend/app/main.py'.",
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
            "name": "query_api",
            "description": (
                "Call the deployed backend API. "
                "Use this for live endpoint behavior, authentication status codes, current database counts, and data-dependent analytics questions. "
                "The tool authenticates the main request with LMS_API_KEY from the environment. "
                "For safe GET or HEAD requests, it may also include an unauthenticated_request field that shows what happens without the Authorization header. "
                "For normal data questions, use the top-level status_code and body. "
                "For questions that explicitly ask about missing authentication, use unauthenticated_request.status_code and unauthenticated_request.body."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "description": "HTTP method such as GET, POST, PUT, PATCH, or DELETE.",
                    },
                    "path": {
                        "type": "string",
                        "description": "API path beginning with '/', for example '/items/' or '/analytics/completion-rate?lab=lab-99'.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Optional JSON request body encoded as a string. Omit it for GET requests.",
                    },
                },
                "required": ["method", "path"],
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


def load_local_env_files() -> None:
    for env_file in ENV_FILES:
        load_env_file(env_file)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    raise RuntimeError(f"Missing required environment variable: {name}")


def normalize_api_base(api_base: str) -> str:
    return api_base.rstrip("/")


def normalize_api_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        raise ValueError("API path must not be empty")

    parts = urlsplit(raw)
    path_part = parts.path or "/"
    if not path_part.startswith("/"):
        path_part = f"/{path_part}"

    return urlunsplit(("", "", path_part, parts.query, parts.fragment))


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


def parse_http_body(raw_body: bytes) -> Any:
    text = raw_body.decode("utf-8", errors="replace")
    stripped = text.strip()
    if not stripped:
        return ""

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def http_request(
    *,
    url: str,
    method: str,
    headers: dict[str, str] | None = None,
    body: str | None = None,
) -> dict[str, Any]:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    data: bytes | None = None
    if body is not None:
        data = body.encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(
        url=url,
        data=data,
        headers=request_headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            return {
                "status_code": response.getcode(),
                "body": parse_http_body(response.read()),
            }
    except urllib.error.HTTPError as exc:
        return {
            "status_code": exc.code,
            "body": parse_http_body(exc.read()),
        }
    except urllib.error.URLError as exc:
        return {
            "status_code": 0,
            "body": {"error": f"Request failed: {exc.reason}"},
        }


def query_api(method: str, path: str, body: str | None = None) -> str:
    load_local_env_files()

    method_normalized = (method or "").strip().upper()
    if not method_normalized:
        return json.dumps({"error": "Tool argument 'method' must be a non-empty string"}, ensure_ascii=False)

    try:
        path_normalized = normalize_api_path(path)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    api_base = normalize_api_base(os.environ.get("AGENT_API_BASE_URL", DEFAULT_AGENT_API_BASE_URL))
    url = f"{api_base}{path_normalized}"

    body_text: str | None
    if body is None:
        body_text = None
    else:
        body_text = body if isinstance(body, str) else json.dumps(body)

    result: dict[str, Any] = {
        "method": method_normalized,
        "path": path_normalized,
    }

    api_key = os.environ.get("LMS_API_KEY", "").strip()
    if api_key:
        authenticated = http_request(
            url=url,
            method=method_normalized,
            headers={"Authorization": f"Bearer {api_key}"},
            body=body_text,
        )
        result.update(authenticated)
        result["auth_used"] = True
    else:
        unauthenticated_only = http_request(
            url=url,
            method=method_normalized,
            headers={},
            body=body_text,
        )
        result.update(unauthenticated_only)
        result["auth_used"] = False
        result["auth_error"] = "Missing required environment variable: LMS_API_KEY"

    if method_normalized in {"GET", "HEAD"} and body_text is None:
        unauthenticated = http_request(
            url=url,
            method=method_normalized,
            headers={},
            body=None,
        )
        result["unauthenticated_request"] = unauthenticated

    return json.dumps(result, ensure_ascii=False)


TOOL_IMPLEMENTATIONS: dict[str, Any] = {
    "read_file": read_file,
    "list_files": list_files,
    "query_api": query_api,
}


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    tool = TOOL_IMPLEMENTATIONS.get(name)
    if tool is None:
        return f"ERROR: Unknown tool: {name}"

    if name in {"read_file", "list_files"}:
        path = arguments.get("path")
        if not isinstance(path, str):
            return "ERROR: Tool argument 'path' must be a string"
        return tool(path)

    if name == "query_api":
        method = arguments.get("method")
        path = arguments.get("path")
        body = arguments.get("body")

        if not isinstance(method, str):
            return "ERROR: Tool argument 'method' must be a string"
        if not isinstance(path, str):
            return "ERROR: Tool argument 'path' must be a string"
        if body is not None and not isinstance(body, str):
            return "ERROR: Tool argument 'body' must be a string when provided"

        return tool(method, path, body)

    return f"ERROR: Unknown tool: {name}"


def call_llm(messages: list[dict[str, Any]]) -> dict[str, Any]:
    load_local_env_files()

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
        if tool_call["tool"] == "query_api":
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

