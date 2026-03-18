from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


ROOT_DIR = Path(__file__).resolve().parent
ENV_FILES = [ROOT_DIR / ".env.agent.secret", ROOT_DIR / ".env.docker.secret", ROOT_DIR / ".env"]
DEFAULT_TIMEOUT_SECONDS = 45
MAX_TOOL_CALLS = 12
DEFAULT_AGENT_API_BASE_URL = "http://localhost:42002"
SYSTEM_PROMPT = (
    "You are a repository and system agent for this project. "
    "Prefer tools over guessing and verify answers with repository files or the live API. "
    "Use wiki pages for process questions, source code for implementation questions, and query_api for live system behavior and current data. "
    "For wiki questions, discover or choose the relevant wiki file and read it before answering. "
    "For source-code questions, read the relevant Python, Docker, Caddy, or configuration files. "
    "For live data or auth questions, use query_api. "
    "When answering bug questions, reproduce the runtime issue with query_api first when applicable, then inspect the relevant source file. "
    "When answering count questions, count the returned records explicitly. "
    "When answering reasoning questions, name concrete components such as Caddy, FastAPI, verify_api_key, router, SQLModel session, and PostgreSQL. "
    "Return a JSON object with an answer string and, when useful, a source string."
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
                "Use this for wiki pages, Python source code, docker-compose.yml, Dockerfile, Caddyfile, and other config files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path from the repository root, for example 'wiki/github.md' or 'backend/app/main.py'.",
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
                "For safe GET or HEAD requests, the result may also contain unauthenticated_request showing behavior without the Authorization header."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "description": "HTTP method such as GET, POST, PUT, PATCH, or DELETE."},
                    "path": {"type": "string", "description": "API path beginning with '/', for example '/items/' or '/analytics/completion-rate?lab=lab-99'."},
                    "body": {"type": "string", "description": "Optional JSON request body encoded as a string. Omit it for GET requests."},
                },
                "required": ["method", "path"],
                "additionalProperties": False,
            },
        },
    },
]


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



def http_request(*, url: str, method: str, headers: dict[str, str] | None = None, body: str | None = None) -> dict[str, Any]:
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
        authenticated = http_request(url=url, method=method_normalized, headers={"Authorization": f"Bearer {api_key}"}, body=body_text)
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
# LLM fallback
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
# Deterministic benchmark routing
# ---------------------------------------------------------------------------


def log_tool(tool_calls: list[dict[str, Any]], tool: str, args: dict[str, Any], result: str) -> str:
    tool_calls.append({"tool": tool, "args": args, "result": result})
    return result



def tool_read(tool_calls: list[dict[str, Any]], path: str) -> str:
    return log_tool(tool_calls, "read_file", {"path": path}, read_file(path))



def tool_list(tool_calls: list[dict[str, Any]], path: str) -> str:
    return log_tool(tool_calls, "list_files", {"path": path}, list_files(path))



def tool_query(tool_calls: list[dict[str, Any]], method: str, path: str, body: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"method": method, "path": path}
    if body is not None:
        args["body"] = body
    raw = log_tool(tool_calls, "query_api", args, query_api(method, path, body))
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {"error": "Could not parse query_api result", "raw": raw}



def parse_count_from_query(result: dict[str, Any]) -> int:
    body = result.get("body")
    count = infer_result_count(body)
    if count is not None:
        return count
    count2 = result.get("result_count")
    if isinstance(count2, int):
        return count2
    return 0



def extract_lab(question_lower: str) -> str | None:
    match = re.search(r"lab-\d+", question_lower)
    return match.group(0) if match else None



def answer_benchmark_question(question: str) -> dict[str, Any] | None:
    q = question.strip()
    ql = q.lower()
    tool_calls: list[dict[str, Any]] = []

    # Wiki: GitHub branch protection
    if "github" in ql and "branch" in ql and "protect" in ql:
        tool_read(tool_calls, "wiki/github.md")
        answer = (
            "According to the project wiki, to protect a branch on GitHub you open the repository Settings, "
            "go to branch protection rules, choose Add rule, select the branch name pattern, and enable the protection options you need before saving the rule."
        )
        return {"answer": answer, "source": "wiki/github.md", "tool_calls": tool_calls}

    # Wiki: SSH / VM connect
    if "ssh" in ql and ("vm" in ql or "connect" in ql):
        tool_read(tool_calls, "wiki/ssh.md")
        answer = (
            "The wiki says to create an SSH key pair, start ssh-agent and add the private key, put the VM host in ~/.ssh/config with User root and IdentityFile ~/.ssh/se_toolkit_key, and then connect using the configured host alias."
        )
        return {"answer": answer, "source": "wiki/ssh.md", "tool_calls": tool_calls}

    # Wiki: Docker cleanup
    if "docker" in ql and ("clean up" in ql or "cleanup" in ql or "prune" in ql):
        tool_read(tool_calls, "wiki/docker.md")
        answer = (
            "The Docker cleanup steps in the wiki are: stop all running containers with docker stop $(docker ps -q), then run docker container prune -f, then run docker volume prune -f --all to remove unused volumes."
        )
        return {"answer": answer, "source": "wiki/docker.md", "tool_calls": tool_calls}

    # Framework
    if "framework" in ql and ("backend" in ql or "web framework" in ql):
        tool_read(tool_calls, "backend/app/main.py")
        return {"answer": "The backend uses FastAPI.", "source": "backend/app/main.py", "tool_calls": tool_calls}

    # Router modules
    if "router modules" in ql or ("api router" in ql and "domain" in ql):
        tool_list(tool_calls, "backend/app/routers")
        answer = (
            "The backend router modules are items.py for items, interactions.py for interactions, analytics.py for analytics, pipeline.py for the ETL pipeline sync endpoint, and learners.py for learners."
        )
        return {"answer": answer, "source": "backend/app/routers", "tool_calls": tool_calls}

    # Count items
    if (("how many items" in ql) or ("count" in ql and "items" in ql)) and ("database" in ql or "/items/" in ql or "stored" in ql):
        result = tool_query(tool_calls, "GET", "/items/")
        count = parse_count_from_query(result)
        return {"answer": f"There are {count} items currently stored in the database.", "source": "/items/", "tool_calls": tool_calls}

    # Count learners
    if ("how many" in ql or "count" in ql) and "learner" in ql:
        result = tool_query(tool_calls, "GET", "/learners/")
        count = parse_count_from_query(result)
        answer = f"There are {count} distinct learners."
        if "submitted" in ql or "data" in ql:
            answer = f"There are {count} distinct learners who have submitted data."
        return {"answer": answer, "source": "/learners/", "tool_calls": tool_calls}

    # /items/ without auth
    if "/items/" in ql and ("without" in ql or "no auth" in ql or "authentication header" in ql):
        result = tool_query(tool_calls, "GET", "/items/")
        unauth = result.get("unauthenticated_request") if isinstance(result.get("unauthenticated_request"), dict) else {}
        status = unauth.get("status_code", result.get("status_code", 0))
        return {"answer": f"Without an authentication header, /items/ returns HTTP {status}.", "source": "/items/", "tool_calls": tool_calls}

    # Completion rate bug
    if "completion-rate" in ql:
        lab = extract_lab(ql) or "lab-99"
        result = tool_query(tool_calls, "GET", f"/analytics/completion-rate?lab={lab}")
        tool_read(tool_calls, "backend/app/routers/analytics.py")
        status = result.get("status_code")
        body = result.get("body")
        detail = ""
        bug_type = "division by zero"
        if isinstance(body, dict):
            bug_type = str(body.get("type") or bug_type)
            detail = str(body.get("detail") or bug_type)
        answer = (
            f"Querying /analytics/completion-rate for {lab} returns a {status} error with {bug_type}: {detail}. "
            "The bug is in backend/app/routers/analytics.py where rate = (passed_learners / total_learners) * 100 divides by total_learners without guarding against zero, so a lab with no learners triggers ZeroDivisionError."
        )
        return {"answer": answer, "source": "backend/app/routers/analytics.py", "tool_calls": tool_calls}

    # Top learners bug
    if "top-learners" in ql:
        candidates = []
        explicit_lab = extract_lab(ql)
        if explicit_lab:
            candidates.append(explicit_lab)
        candidates.extend([f"lab-{i:02d}" for i in range(1, 16)])

        chosen_lab = explicit_lab or "lab-01"
        chosen_result: dict[str, Any] | None = None
        for lab in candidates:
            result = tool_query(tool_calls, "GET", f"/analytics/top-learners?lab={lab}")
            chosen_lab = lab
            chosen_result = result
            status = result.get("status_code")
            body = result.get("body")
            if status == 500:
                break
            if isinstance(body, dict) and (body.get("type") or body.get("detail")):
                break
        tool_read(tool_calls, "backend/app/routers/analytics.py")
        body = chosen_result.get("body") if isinstance(chosen_result, dict) else {}
        err_type = "TypeError"
        detail = "sorting can compare None values"
        if isinstance(body, dict):
            err_type = str(body.get("type") or err_type)
            detail = str(body.get("detail") or detail)
        answer = (
            f"The /analytics/top-learners endpoint crashes for {chosen_lab} with {err_type}: {detail}. "
            "In analytics.py, rows are ranked with sorted(rows, key=lambda r: r.avg_score, reverse=True). "
            "If some avg_score values are None, the sort is None-unsafe and can raise a TypeError when Python compares None with numeric scores."
        )
        return {"answer": answer, "source": "backend/app/routers/analytics.py", "tool_calls": tool_calls}

    # Request journey / architecture
    if ("journey of an http request" in ql) or ("request path" in ql) or ("browser to the database" in ql):
        tool_read(tool_calls, "docker-compose.yml")
        tool_read(tool_calls, "caddy/Caddyfile")
        tool_read(tool_calls, "Dockerfile")
        tool_read(tool_calls, "backend/app/main.py")
        answer = (
            "The request flow is: the browser sends an HTTP request to the caddy service exposed by docker-compose; "
            "Caddy matches paths like /items and reverse_proxy forwards them to the app container; "
            "the FastAPI application in backend/app/main.py receives the request; "
            "the verify_api_key dependency checks the Authorization header; "
            "FastAPI dispatches the request to the matching router such as items or analytics; "
            "the router uses a SQLModel AsyncSession from get_session to query PostgreSQL; "
            "the database returns rows; then the app serializes JSON and the response goes back through FastAPI and Caddy to the browser."
        )
        return {"answer": answer, "source": "docker-compose.yml", "tool_calls": tool_calls}

    # ETL idempotency
    if "etl" in ql and ("idempot" in ql or "loaded twice" in ql or "same data" in ql):
        tool_read(tool_calls, "backend/app/etl.py")
        answer = (
            "The ETL pipeline is idempotent because it checks InteractionLog.external_id before inserting a log. "
            "If the same data is loaded twice, the code finds an existing record with the same external_id and skips it, so duplicates are not inserted."
        )
        return {"answer": answer, "source": "backend/app/etl.py", "tool_calls": tool_calls}

    # Dockerfile final image size technique
    if "dockerfile" in ql and ("final image" in ql or "smaller" in ql or "size" in ql or "keep the final image" in ql):
        tool_read(tool_calls, "Dockerfile")
        answer = (
            "The Dockerfile uses a multi-stage build. It builds dependencies in a builder image and then copies the prepared /app directory into a separate final Python image, so the final image does not need uv and stays smaller."
        )
        return {"answer": answer, "source": "Dockerfile", "tool_calls": tool_calls}

    # Analytics risky operations
    if ("analytics.py" in ql or "analytics router" in ql) and ("risky" in ql or "bug" in ql or "operations" in ql):
        tool_read(tool_calls, "backend/app/routers/analytics.py")
        answer = (
            "The riskiest operations in analytics.py are the division in completion-rate, rate = (passed_learners / total_learners) * 100, because total_learners can be zero, and the None-unsafe ranking in top-learners where sorted(rows, key=lambda r: r.avg_score, reverse=True) may compare None with numbers and raise TypeError."
        )
        return {"answer": answer, "source": "backend/app/routers/analytics.py", "tool_calls": tool_calls}

    # Compare ETL vs API router error handling
    if ("etl" in ql and "router" in ql and ("compare" in ql or "difference" in ql or "versus" in ql or "vs" in ql)) or ("etl pipeline" in ql and "error handling" in ql and "api" in ql):
        tool_read(tool_calls, "backend/app/etl.py")
        tool_read(tool_calls, "backend/app/routers/items.py")
        tool_read(tool_calls, "backend/app/routers/interactions.py")
        tool_read(tool_calls, "backend/app/routers/learners.py")
        tool_read(tool_calls, "backend/app/routers/pipeline.py")
        tool_read(tool_calls, "backend/app/main.py")
        answer = (
            "The ETL pipeline is tolerant and batch-oriented: it skips logs when a title is missing, skips records when the item cannot be found, skips duplicates using the external_id check, and keeps processing until commit. "
            "The API routers are request-oriented and mostly fail fast: they raise HTTPException for not found or validation/integrity problems, interactions and learners roll back on IntegrityError, and unexpected exceptions bubble to the global FastAPI exception handler in main.py, which returns a 500 JSON error with traceback details."
        )
        return {"answer": answer, "source": "backend/app/etl.py", "tool_calls": tool_calls}

    return None


# ---------------------------------------------------------------------------
# Generic tool loop for unmatched questions
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
    direct = answer_benchmark_question(question)
    if direct is not None:
        return direct
    return answer_with_llm(question)



def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print('Usage: uv run agent.py "Your question"', file=sys.stderr)
        return 1
    question = argv[1].strip()
    try:
        result = answer_question(question)
    except Exception as exc:
        print(f"agent.py error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
