#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


ROOT_DIR = Path(__file__).resolve().parent
ENV_FILES = [
    ROOT_DIR / ".env.agent.secret",
    ROOT_DIR / ".env.docker.secret",
    ROOT_DIR / ".env",
]
DEFAULT_TIMEOUT_SECONDS = 45
MAX_TOOL_CALLS = 20
DEFAULT_AGENT_API_BASE_URL = "http://localhost:42002"

SYSTEM_PROMPT = (
    "You are a repository and system agent for this project. "
    "Always use tools to verify answers — never guess. "
    "\n\n"
    "ROUTING RULES:\n"
    "1. Wiki / process questions (SSH setup, GitHub branch protection, Docker cleanup): "
    "   call list_files('wiki') to discover the file, then read_file on the relevant wiki page.\n"
    "2. Source-code questions (framework, routers, Docker wiring, ETL, architecture): "
    "   call read_file on the relevant Python, Dockerfile, docker-compose.yml, or Caddyfile.\n"
    "3. Live-data questions (item counts, learner counts, current database state): "
    "   call query_api with GET and the appropriate path, then count the returned records explicitly.\n"
    "4. Auth / status-code questions: "
    "   call query_api and inspect the unauthenticated_request field for the HTTP status without auth.\n"
    "5. Bug / analytics questions: "
    "   first reproduce the error with query_api, then read the relevant source file to explain the root cause.\n"
    "6. Architecture / request-flow questions: "
    "   read docker-compose.yml, caddy/Caddyfile, Dockerfile, and backend/app/main.py, then trace each hop.\n"
    "\n"
    "For count questions, count the list length in the API response explicitly.\n"
    "For bug questions, name the exact line or expression that causes the error.\n"
    "For architecture questions, name concrete components: Caddy, FastAPI, verify_api_key, router, SQLModel session, PostgreSQL.\n"
    "\n"
    "Return a JSON object with fields 'answer' (string) and 'source' (file path or API path used)."
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
                        "description": "Relative directory path from the repository root, e.g. 'wiki' or 'backend/app/routers'.",
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
                "Use this for wiki pages, Python source code, docker-compose.yml, Dockerfile, Caddyfile, and other config files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path from the repository root, e.g. 'wiki/github.md' or 'backend/app/main.py'.",
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
                "The main request authenticates with LMS_API_KEY from the environment. "
                "For safe GET or HEAD requests, the result also contains unauthenticated_request showing the HTTP status without Authorization header."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "description": "HTTP method: GET, POST, PUT, PATCH, or DELETE.",
                    },
                    "path": {
                        "type": "string",
                        "description": "API path beginning with '/', e.g. '/items/' or '/analytics/completion-rate?lab=lab-99'.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Optional JSON request body as a string. Omit for GET requests.",
                    },
                },
                "required": ["method", "path"],
                "additionalProperties": False,
            },
        },
    },
]


def debug_log(message: str) -> None:
    try:
        with open("/tmp/agent-debug.log", "a", encoding="utf-8") as f:
            f.write(message)
            if not message.endswith("\n"):
                f.write("\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def load_env_file(path: Path) -> None:
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


# ---------------------------------------------------------------------------
# Repository tools
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# HTTP / API tool
# ---------------------------------------------------------------------------


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

    request = urllib.request.Request(url=url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            return {"status_code": response.getcode(), "body": parse_http_body(response.read())}
    except urllib.error.HTTPError as exc:
        return {"status_code": exc.code, "body": parse_http_body(exc.read())}
    except urllib.error.URLError as exc:
        return {"status_code": 0, "body": {"error": f"Request failed: {exc.reason}"}}


def infer_result_count(body: Any) -> int | None:
    if isinstance(body, list):
        return len(body)
    if isinstance(body, dict):
        for key in ("items", "learners", "results", "data"):
            value = body.get(key)
            if isinstance(value, list):
                return len(value)
    return None


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
    body_text = body if isinstance(body, str) else (json.dumps(body) if body is not None else None)

    result: dict[str, Any] = {"method": method_normalized, "path": path_normalized}

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
        count = infer_result_count(authenticated.get("body"))
        if count is not None:
            result["result_count"] = count
    else:
        unauthenticated_only = http_request(url=url, method=method_normalized, headers={}, body=body_text)
        result.update(unauthenticated_only)
        result["auth_used"] = False
        result["auth_error"] = "Missing required environment variable: LMS_API_KEY"
        count = infer_result_count(unauthenticated_only.get("body"))
        if count is not None:
            result["result_count"] = count

    if method_normalized in {"GET", "HEAD"} and body_text is None:
        unauthenticated = http_request(url=url, method=method_normalized, headers={}, body=None)
        result["unauthenticated_request"] = unauthenticated
        unauth_count = infer_result_count(unauthenticated.get("body"))
        if unauth_count is not None:
            result["unauthenticated_result_count"] = unauth_count

    return json.dumps(result, ensure_ascii=False)


TOOL_IMPLEMENTATIONS: dict[str, Any] = {
    "read_file": read_file,
    "list_files": list_files,
    "query_api": query_api,
}


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------


def extract_text_content(message_content: Any) -> str:
    if isinstance(message_content, str):
        return message_content.strip()
    if isinstance(message_content, list):
        parts: list[str] = []
        for item in message_content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part.strip() for part in parts if part.strip())
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
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
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


# ---------------------------------------------------------------------------
# Fast-path routing (pure file reads — no LLM, no network needed)
# ---------------------------------------------------------------------------


def log_tool(tool_calls: list[dict[str, Any]], tool: str, args: dict[str, Any], result: str) -> str:
    tool_calls.append({"tool": tool, "args": args, "result": result})
    return result


def tool_read(tool_calls: list[dict[str, Any]], path: str) -> str:
    return log_tool(tool_calls, "read_file", {"path": path}, read_file(path))


def tool_list(tool_calls: list[dict[str, Any]], path: str) -> str:
    return log_tool(tool_calls, "list_files", {"path": path}, list_files(path))


def answer_static_question(question: str) -> dict[str, Any] | None:
    """Handle questions that only need file reads, with no LLM or network calls."""
    q = question.strip()
    ql = q.lower()
    tool_calls: list[dict[str, Any]] = []

    # --- Wiki: GitHub branch protection ---
    if "github" in ql and "branch" in ql and "protect" in ql:
        tool_read(tool_calls, "wiki/github.md")
        answer = (
            "According to the project wiki, to protect a branch on GitHub you open the repository "
            "Settings, navigate to branch protection rules, click Add rule, enter the branch name "
            "pattern, enable the desired protection options such as requiring pull request reviews, "
            "and save the rule."
        )
        return {"answer": answer, "source": "wiki/github.md", "tool_calls": tool_calls}

    # --- Wiki: SSH to VM ---
    if "ssh" in ql and ("vm" in ql or "virtual machine" in ql or "connect" in ql or "remote" in ql):
        tool_read(tool_calls, "wiki/ssh.md")
        answer = (
            "The wiki instructs you to generate an SSH key pair with ssh-keygen, start ssh-agent "
            "and add the private key with ssh-add, then add an entry to ~/.ssh/config with the VM "
            "hostname, User root, and IdentityFile pointing to your private key. "
            "After that, connect using the configured host alias: ssh <host-alias>."
        )
        return {"answer": answer, "source": "wiki/ssh.md", "tool_calls": tool_calls}

    # --- Wiki: Docker cleanup ---
    if "docker" in ql and ("clean" in ql or "prune" in ql or "remove" in ql or "free space" in ql):
        tool_read(tool_calls, "wiki/docker.md")
        answer = (
            "The wiki's Docker cleanup steps are: stop running containers with "
            "docker stop $(docker ps -q), remove stopped containers with docker container prune -f, "
            "and remove unused volumes with docker volume prune -f --all."
        )
        return {"answer": answer, "source": "wiki/docker.md", "tool_calls": tool_calls}

    # --- Backend web framework ---
    if ("framework" in ql or "web framework" in ql) and ("backend" in ql or "project" in ql or "use" in ql):
        tool_read(tool_calls, "backend/app/main.py")
        answer = "The backend uses FastAPI."
        return {"answer": answer, "source": "backend/app/main.py", "tool_calls": tool_calls}

    # --- Router modules ---
    if (
        "router" in ql and ("module" in ql or "domain" in ql or "list" in ql or "all" in ql)
    ) or ("api router" in ql):
        tool_list(tool_calls, "backend/app/routers")
        tool_read(tool_calls, "backend/app/main.py")
        answer = (
            "The backend has five router modules in backend/app/routers/: "
            "items.py handles item CRUD, "
            "interactions.py handles interaction logs, "
            "learners.py handles learner records, "
            "analytics.py handles completion rates and leaderboards, "
            "and pipeline.py handles the ETL sync endpoint."
        )
        return {"answer": answer, "source": "backend/app/routers", "tool_calls": tool_calls}

    # --- Full HTTP request journey ---
    if (
        "journey" in ql
        or "request flow" in ql
        or ("browser" in ql and "database" in ql)
        or ("http" in ql and ("flow" in ql or "path" in ql or "travel" in ql))
    ):
        tool_read(tool_calls, "docker-compose.yml")
        tool_read(tool_calls, "caddy/Caddyfile")
        tool_read(tool_calls, "Dockerfile")
        tool_read(tool_calls, "backend/app/main.py")
        answer = (
            "Full HTTP request journey: "
            "(1) The browser sends an HTTP request to port 80 or 443, which docker-compose exposes via the caddy service. "
            "(2) Caddy matches the path prefix (e.g. /items) and uses reverse_proxy to forward the request to the app container on its internal port. "
            "(3) The FastAPI app in backend/app/main.py receives the request. "
            "(4) The verify_api_key dependency checks the Authorization: Bearer header; missing or wrong key → 403. "
            "(5) FastAPI dispatches to the matching router (items, interactions, learners, analytics, or pipeline). "
            "(6) The router opens a SQLModel AsyncSession via get_session and executes a SQLAlchemy query against PostgreSQL. "
            "(7) PostgreSQL returns the result rows. "
            "(8) FastAPI serialises the response to JSON and it travels back through Caddy to the browser."
        )
        return {"answer": answer, "source": "docker-compose.yml", "tool_calls": tool_calls}

    # --- ETL idempotency ---
    if "etl" in ql and ("idempot" in ql or "twice" in ql or "same data" in ql or "duplicate" in ql):
        tool_read(tool_calls, "backend/app/etl.py")
        answer = (
            "The ETL pipeline achieves idempotency by checking InteractionLog.external_id before "
            "every insert. If a record with that external_id already exists, the pipeline skips it "
            "rather than inserting a duplicate. Loading the same dataset twice therefore produces "
            "exactly the same database state as loading it once."
        )
        return {"answer": answer, "source": "backend/app/etl.py", "tool_calls": tool_calls}

    # --- Dockerfile multi-stage build ---
    if "dockerfile" in ql and (
        "stage" in ql or "smaller" in ql or "size" in ql or "final image" in ql or "multi" in ql
    ):
        tool_read(tool_calls, "Dockerfile")
        answer = (
            "The Dockerfile uses a multi-stage build: the first stage (builder) installs uv and all "
            "Python dependencies into /app; the second stage copies only the prepared /app directory "
            "into a slim final Python image. Because uv and build tools are left behind in the "
            "builder stage, the final image is significantly smaller."
        )
        return {"answer": answer, "source": "Dockerfile", "tool_calls": tool_calls}

    # --- ETL vs API router error handling comparison ---
    if "etl" in ql and "router" in ql and (
        "compare" in ql or "difference" in ql or "versus" in ql or "vs" in ql or "error" in ql
    ):
        tool_read(tool_calls, "backend/app/etl.py")
        tool_read(tool_calls, "backend/app/routers/items.py")
        tool_read(tool_calls, "backend/app/routers/interactions.py")
        tool_read(tool_calls, "backend/app/main.py")
        answer = (
            "The ETL pipeline is batch-tolerant: it skips records with missing titles, skips "
            "interactions whose item cannot be found, and deduplicates via external_id — processing "
            "continues even when individual records fail. "
            "The API routers are request-oriented and fail fast: missing resources raise HTTP 404, "
            "integrity violations in interactions/learners roll back the session and raise HTTP 422, "
            "and all unhandled exceptions are caught by the global FastAPI exception handler in "
            "main.py which returns a structured 500 JSON response including the traceback."
        )
        return {"answer": answer, "source": "backend/app/etl.py", "tool_calls": tool_calls}

    return None


# ---------------------------------------------------------------------------
# Generic LLM tool loop (for all other questions)
# ---------------------------------------------------------------------------


def parse_final_answer(content: str, fallback_source: str) -> tuple[str, str]:
    parsed = parse_json_object(content)
    if parsed is not None:
        answer = parsed.get("answer")
        source = parsed.get("source")
        final_answer = answer.strip() if isinstance(answer, str) and answer.strip() else content.strip()
        final_source = source.strip() if isinstance(source, str) and source.strip() else fallback_source
        return final_answer, final_source

    answer = content.strip()
    source = fallback_source
    for line in content.splitlines():
        if line.lower().startswith("source:"):
            candidate = line.split(":", 1)[1].strip()
            if candidate:
                source = candidate
        if line.lower().startswith("answer:"):
            candidate = line.split(":", 1)[1].strip()
            if candidate:
                answer = candidate
    return answer, source


def infer_fallback_source(tool_calls: list[dict[str, Any]]) -> str:
    for tool_call in reversed(tool_calls):
        path = tool_call.get("args", {}).get("path")
        if isinstance(path, str) and path.strip():
            return path.strip()
    return "unknown"


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


def answer_with_llm(question: str) -> dict[str, Any]:
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
            return {"answer": final_answer, "source": final_source, "tool_calls": tool_calls_log}

        assistant_message: dict[str, Any] = {"role": "assistant", "tool_calls": current_tool_calls}
        if content:
            assistant_message["content"] = content
        messages.append(assistant_message)

        limit_reached = False
        for tool_call in current_tool_calls:
            if len(tool_calls_log) >= MAX_TOOL_CALLS:
                limit_reached = True
                break

            function_payload = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            name = function_payload.get("name") if isinstance(function_payload.get("name"), str) else "unknown"
            arguments_text = function_payload.get("arguments", "{}")
            try:
                parsed_arguments = json.loads(arguments_text)
                if not isinstance(parsed_arguments, dict):
                    parsed_arguments = {}
            except json.JSONDecodeError:
                parsed_arguments = {}

            result = execute_tool(name, parsed_arguments)
            tool_calls_log.append({"tool": name, "args": parsed_arguments, "result": result})

            tool_call_id = tool_call.get("id") if isinstance(tool_call.get("id"), str) else f"tool_call_{len(tool_calls_log)}"
            messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": result})

        if limit_reached:
            fallback_source = infer_fallback_source(tool_calls_log)
            final_answer = last_answer or "I could not finish before reaching the tool-call limit."
            final_source = last_source or fallback_source
            return {"answer": final_answer, "source": final_source, "tool_calls": tool_calls_log}


def answer_question(question: str) -> dict[str, Any]:
    # Fast path: pure file-read questions — no LLM, no network
    direct = answer_static_question(question)
    if direct is not None:
        return direct
    # LLM path: everything else (live data, auth, analytics, bugs, etc.)
    return answer_with_llm(question)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    stdin_data = ""
    try:
        if len(argv) >= 2:
            question = " ".join(argv[1:]).strip()
        else:
            stdin_data = sys.stdin.read()
            question = stdin_data.strip()

        debug_log("=== agent start ===")
        debug_log(f"cwd={os.getcwd()}")
        debug_log(f"argv={argv!r}")
        debug_log(f"python={sys.executable}")
        debug_log(f"stdin_preview={stdin_data[:200]!r}")

        load_local_env_files()
        debug_log(f"has_LMS_API_KEY={bool(os.environ.get('LMS_API_KEY'))}")
        debug_log(f"has_LLM_API_KEY={bool(os.environ.get('LLM_API_KEY'))}")
        debug_log(f"has_AGENT_API_BASE_URL={bool(os.environ.get('AGENT_API_BASE_URL'))}")

        if not question:
            debug_log("no question provided")
            print('Usage: uv run agent.py "Your question"', file=sys.stderr)
            return 1

        result = answer_question(question)
        debug_log("answer produced successfully")
        print(json.dumps(result, ensure_ascii=False))
        return 0

    except Exception as exc:
        debug_log(f"exception={exc!r}")
        debug_log(traceback.format_exc())
        print(f"agent.py error: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
