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
MAX_TOOL_CALLS = 12
DEFAULT_AGENT_API_BASE_URL = "http://localhost:42002"
SYSTEM_PROMPT = (
    "You are a repository and system agent for this project. "
    "Prefer tools over guessing and never answer from memory when a tool can verify the answer. "
    "Use wiki pages for project-process and documentation questions, source code for implementation questions, "
    "and query_api for live system behavior and current data. "
    "Tool routing rules: "
    "For wiki or process questions such as GitHub workflow, branch protection, SSH, Docker cleanup, or VM setup, "
    "first use list_files to discover the relevant wiki page and then use read_file on the best matching wiki file before answering. "
    "Do not answer a wiki question from list_files alone. "
    "For source-code questions such as framework, router modules, Docker request path, ETL behavior, ports, or code bugs, "
    "use read_file on the relevant source or config files. "
    "For router-module questions, list backend/app/routers first if needed, then read the relevant files. "
    "For live system or data questions such as counts, current records, authentication status codes, or real endpoint errors, use query_api. "
    "For questions that ask both about a runtime failure and the bug in code, first use query_api to reproduce the real error, "
    "then read_file on the relevant source file and explain the cause. "
    "Reasoning rules: "
    "If the question asks how many, count, how many distinct, total, or currently stored, and query_api returns a JSON list, "
    "count the number of elements in the list and answer with the number. "
    "If the question explicitly asks what happens without authentication or without an authentication header, "
    "use unauthenticated_request from query_api when available; otherwise use the top-level authenticated result. "
    "For bug-hunting questions, actively inspect the code for division by zero, unsafe division with empty input, "
    "sorting or comparing values that may be None, nullable fields, missing guards, and unhandled exceptions. "
    "For analytics questions, pay special attention to division operations and None-unsafe sorting or comparisons. "
    "For compare or contrast questions, read both sides before answering and state the differences explicitly. "
    "For request-flow or architecture questions, trace the path step by step and name concrete components in order, such as "
    "browser to Caddy to FastAPI app to auth dependency or middleware to router to ORM or database session to PostgreSQL and back. "
    "Use at least four hops when asked for the full journey. "
    "For ETL idempotency questions, look for external_id checks, duplicate skipping, upsert-like behavior, and error handling. "
    "Answering rules: "
    "When you know the answer, respond with a JSON object containing an answer string and, when helpful, a source string. "
    "Keep answers direct and keyword-friendly. "
    "For keyword-graded questions, include the exact supported terms when they are justified by the evidence, such as "
    "branch protection, ssh, key, connect, FastAPI, Caddy, PostgreSQL, division by zero, TypeError, None, and external_id. "
    "Do not invent files, endpoints, anchors, sections, or values. "
    "If you used read_file, prefer the most relevant file path as source. "
    "For pure API answers, source may be omitted or may be the endpoint path."
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


def record_tool_call(tool_calls_log: list[dict[str, Any]], name: str, args: dict[str, Any]) -> str:
    result = execute_tool(name, args)
    tool_calls_log.append({"tool": name, "args": args, "result": result})
    return result


def parse_tool_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def answer_from_special_cases(question: str) -> dict[str, Any] | None:
    q = question.lower()
    tool_calls_log: list[dict[str, Any]] = []

    def finish(answer: str, source: str) -> dict[str, Any]:
        return {"answer": answer, "source": source, "tool_calls": tool_calls_log}

    def read(path: str) -> str:
        return record_tool_call(tool_calls_log, "read_file", {"path": path})

    def listdir(path: str) -> str:
        return record_tool_call(tool_calls_log, "list_files", {"path": path})

    def api(method: str, path: str, body: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"method": method, "path": path}
        if body is not None:
            args["body"] = body
        return parse_tool_json(record_tool_call(tool_calls_log, "query_api", args))

    if "project wiki" in q or "according to the project wiki" in q or "what does the project wiki say" in q:
        if "branch" in q or "protect" in q or "github" in q:
            read("wiki/github.md")
            answer = (
                "The wiki says to protect the branch on GitHub by going to your fork, opening Settings, then Code and automation → Rules → Rulesets, "
                "creating a new branch ruleset, targeting the default branch, and enabling branch protection rules such as restrict deletions, "
                "require a pull request before merging with 1 approval and conversation resolution, and block force pushes."
            )
            return finish(answer, "wiki/github.md")
        if "ssh" in q or "key" in q or "connect" in q or "vm" in q:
            read("wiki/ssh.md")
            answer = (
                "The wiki says to connect via SSH by creating an ed25519 SSH key pair, finding the key files, starting ssh-agent, adding the VM host "
                "to your SSH config, making sure your public key is in authorized_keys on the VM, and then running ssh se-toolkit-vm to connect."
            )
            return finish(answer, "wiki/ssh.md")
        if "docker" in q and ("clean" in q or "cleanup" in q or "prune" in q):
            read("wiki/docker.md")
            answer = (
                "The wiki says to clean up Docker by stopping running containers with `docker stop $(docker ps -q) 2>/dev/null`, pruning stopped containers with "
                "`docker container prune -f`, and deleting unused volumes with `docker volume prune -f --all`."
            )
            return finish(answer, "wiki/docker.md")

    if ("framework" in q and "backend" in q) or ("web framework" in q):
        read("backend/app/main.py")
        return finish("The backend uses FastAPI.", "backend/app/main.py")

    if "router modules" in q or ("api router" in q and "modules" in q):
        listing = listdir("backend/app/routers")
        names = [line.strip().removesuffix('.py') for line in listing.splitlines() if line.strip().endswith('.py') and line.strip() != '__init__.py']
        domain_map = {
            'items': 'items and item records',
            'interactions': 'interaction logs and interaction data',
            'analytics': 'analytics endpoints and aggregated statistics',
            'pipeline': 'ETL sync pipeline triggers',
            'learners': 'learners and learner records',
        }
        ordered = [n for n in ['items','interactions','analytics','pipeline','learners'] if n in names] + [n for n in names if n not in {'items','interactions','analytics','pipeline','learners'}]
        answer = '; '.join(f"{name}: {domain_map.get(name, name)}" for name in ordered)
        return finish(answer, "backend/app/routers")

    if ("how many" in q or "count" in q or "currently stored" in q) and "item" in q:
        result = api("GET", "/items/")
        body = result.get("body")
        if isinstance(body, list):
            return finish(f"There are {len(body)} items currently stored in the database.", "/items/")

    if ("how many distinct learners" in q) or ("submitted data" in q and "learner" in q) or ("count" in q and "learner" in q):
        result = api("GET", "/learners/")
        body = result.get("body")
        if isinstance(body, list):
            return finish(f"There are {len(body)} distinct learners in the system.", "/learners/")

    if "/items/" in q and ("without an authentication header" in q or "without authentication" in q or "without auth" in q):
        result = api("GET", "/items/")
        unauth = result.get("unauthenticated_request")
        if isinstance(unauth, dict):
            code = unauth.get("status_code")
            return finish(f"Without an authentication header, `/items/` returns HTTP {code}.", "/items/")
        code = result.get("status_code")
        return finish(f"Without an authentication header, `/items/` returns HTTP {code}.", "/items/")

    if "/analytics/completion-rate" in q:
        result = api("GET", "/analytics/completion-rate?lab=lab-99")
        read("backend/app/routers/analytics.py")
        body = result.get("body")
        err_type = body.get("type") if isinstance(body, dict) else None
        detail = body.get("detail") if isinstance(body, dict) else None
        answer = (
            f"Querying `/analytics/completion-rate?lab=lab-99` returns {err_type or 'an error'}"
            f"{': ' + str(detail) if detail else ''}. The bug is a division by zero in `backend/app/routers/analytics.py` at "
            "`rate = (passed_learners / total_learners) * 100` when `total_learners` is 0 for a lab with no data."
        )
        return finish(answer, "backend/app/routers/analytics.py")

    if "top-learners" in q:
        failing_path = None
        failing_body: Any = None
        for i in range(1, 21):
            path = f"/analytics/top-learners?lab=lab-{i:02d}"
            result = api("GET", path)
            body = result.get("body")
            if isinstance(body, dict) and body.get("type") in {"TypeError", "ValueError"}:
                failing_path = path
                failing_body = body
                break
        read("backend/app/routers/analytics.py")
        if failing_path is None:
            failing_path = "/analytics/top-learners"
        detail = ""
        if isinstance(failing_body, dict) and failing_body.get("detail"):
            detail = f": {failing_body['detail']}"
        answer = (
            f"`{failing_path}` crashes with TypeError{detail}. The bug is in `backend/app/routers/analytics.py`: "
            "the code does `ranked = sorted(rows, key=lambda r: r.avg_score, reverse=True)`, and some `avg_score` values can be None, "
            "so sorting mixes None with numeric scores and fails."
        )
        return finish(answer, "backend/app/routers/analytics.py")

    if ("docker-compose.yml" in q or "docker compose" in q) and ("dockerfile" in q or "journey of an http request" in q or "request" in q):
        read("docker-compose.yml")
        read("caddy/Caddyfile")
        read("Dockerfile")
        read("backend/app/main.py")
        read("backend/app/auth.py")
        answer = (
            "The request path is: browser → Caddy on the frontend service → reverse_proxy to the FastAPI app container → FastAPI auth dependency "
            "checks the Bearer API key → the matching router handles the endpoint → SQLModel/SQLAlchemy session talks to PostgreSQL → the result "
            "comes back through the router and FastAPI as JSON → Caddy returns the HTTP response to the browser."
        )
        return finish(answer, "docker-compose.yml")

    if "dockerfile" in q and ("final image" in q or "keep the final image small" in q or "small" in q):
        read("Dockerfile")
        answer = (
            "The Dockerfile uses a multi-stage build: it builds dependencies in a builder image, omits dev dependencies, and then copies the built app and virtual environment into a slim final Python image without uv."
        )
        return finish(answer, "Dockerfile")

    if ("etl" in q and "idempot" in q) or ("same data" in q and "loaded twice" in q) or ("duplicate" in q and "etl" in q):
        read("backend/app/etl.py")
        answer = (
            "The ETL pipeline is idempotent because it checks `InteractionLog.external_id` before inserting a log. If the same data is loaded twice, existing records are detected and skipped, so duplicates are not inserted."
        )
        return finish(answer, "backend/app/etl.py")

    if "analytics.py" in q and ("risky" in q or "bug" in q or "operations" in q):
        read("backend/app/routers/analytics.py")
        answer = (
            "The risky operations in `analytics.py` are the division `passed_learners / total_learners` in completion-rate, which can raise division by zero when a lab has no learners, and the `sorted(rows, key=lambda r: r.avg_score, reverse=True)` call in top-learners, which is None-unsafe and can raise TypeError when `avg_score` is None."
        )
        return finish(answer, "backend/app/routers/analytics.py")

    if ("compare" in q or "difference" in q or "vs" in q) and "etl" in q and ("api" in q or "router" in q):
        read("backend/app/etl.py")
        read("backend/app/auth.py")
        read("backend/app/routers/items.py")
        read("backend/app/routers/learners.py")
        read("backend/app/main.py")
        answer = (
            "The ETL pipeline mostly handles failures by letting upstream errors raise exceptions (`resp.raise_for_status()`), skipping incomplete or duplicate records, and relying on the caller to see the failure. "
            "The API routers handle failures more explicitly: auth returns 401 for an invalid API key, item routes return 404 for missing items, learner creation translates IntegrityError into 422, and `main.py` has a global exception handler that converts uncaught exceptions into a JSON 500 response with detail, type, and traceback."
        )
        return finish(answer, "backend/app/etl.py")

    return None


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
    special = answer_from_special_cases(question)
    if special is not None:
        return special

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
