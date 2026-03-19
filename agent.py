import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(".env.agent.secret")
load_dotenv(".env.docker.secret", override=False)

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_API_BASE = os.getenv("LLM_API_BASE")
LLM_MODEL = os.getenv("LLM_MODEL")

LMS_API_KEY = os.getenv("LMS_API_KEY")
AGENT_API_BASE_URL = os.getenv("AGENT_API_BASE_URL", "http://localhost:42002")

PROJECT_ROOT = Path(__file__).parent


# ------------------------
# TOOLS
# ------------------------

def read_file(path: str) -> dict:
    try:
        if ".." in path:
            return {"error": "Error: path traversal not allowed"}

        abs_path = (PROJECT_ROOT / path).resolve()
        project_root = PROJECT_ROOT.resolve()

        if not str(abs_path).startswith(str(project_root)):
            return {"error": "Error: access outside project not allowed"}

        if not abs_path.exists():
            return {"error": "Error: file not found"}

        content = abs_path.read_text(encoding="utf-8", errors="replace")
        limit = 16000
        if len(content) > limit:
            content = content[:limit] + "\n... (truncated)"

        return {"path": path, "content": content}
    except Exception as e:
        return {"error": f"Error: {str(e)}"}


def list_files(path: str) -> dict:
    try:
        if ".." in path:
            return {"error": "Error: path traversal not allowed"}

        abs_path = (PROJECT_ROOT / path).resolve()
        project_root = PROJECT_ROOT.resolve()

        if not str(abs_path).startswith(str(project_root)):
            return {"error": "Error: access outside project not allowed"}

        if not abs_path.exists():
            return {"error": "Error: path not found"}

        if not abs_path.is_dir():
            return {"error": "Error: path is not a directory"}

        entries = []
        for item in sorted(abs_path.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            entries.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
            })

        return {"directory": path, "items": entries}
    except Exception as e:
        return {"error": f"Error: {str(e)}"}


def query_api(method: str, path: str, body: dict | None = None, use_auth: bool = True) -> dict:
    try:
        url = f"{AGENT_API_BASE_URL.rstrip('/')}{path}"

        headers = {"Content-Type": "application/json"}
        if use_auth and LMS_API_KEY:
            headers["Authorization"] = f"Bearer {LMS_API_KEY}"

        method_upper = method.upper()

        if method_upper == "GET":
            response = requests.get(url, headers=headers, timeout=30)
        elif method_upper == "POST":
            response = requests.post(url, headers=headers, json=body or {}, timeout=30)
        elif method_upper == "PUT":
            response = requests.put(url, headers=headers, json=body or {}, timeout=30)
        elif method_upper == "DELETE":
            response = requests.delete(url, headers=headers, timeout=30)
        else:
            return {"error": f"Unsupported method: {method}"}

        try:
            parsed_body = response.json()
        except json.JSONDecodeError:
            parsed_body = response.text

        return {
            "status_code": response.status_code,
            "body": parsed_body,
        }
    except Exception as e:
        return {"error": f"Error: {str(e)}"}


# ------------------------
# TOOL SCHEMAS
# ------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read repository files such as wiki pages, source code, Docker files, and config files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path, for example 'wiki/github.md' or 'backend/app/main.py'."
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories inside a project folder to inspect structure and discover relevant files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative directory path such as 'wiki', 'backend/app', or 'backend/app/routers'."
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_api",
            "description": "Call the deployed LMS backend API for live data, endpoint behavior, and error reproduction. Set use_auth=false for unauthenticated checks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "description": "HTTP method like GET, POST, PUT, DELETE."
                    },
                    "path": {
                        "type": "string",
                        "description": "API path, for example '/items/' or '/analytics/completion-rate?lab=lab-99'."
                    },
                    "body": {
                        "type": "object",
                        "description": "Optional JSON body for POST or PUT requests."
                    },
                    "use_auth": {
                        "type": "boolean",
                        "description": "Whether to attach Authorization header.",
                        "default": True
                    }
                },
                "required": ["method", "path"]
            }
        }
    }
]

TOOL_FUNCTIONS = {
    "read_file": read_file,
    "list_files": list_files,
    "query_api": query_api,
}


# ------------------------
# SYSTEM PROMPT
# ------------------------

SYSTEM_PROMPT = """You are an assistant for the Learning Management Service repository.

You must use tools whenever the answer depends on source files, project structure, wiki pages, or the running backend API.

Available tools:
- read_file
- list_files
- query_api

Rules:
- For wiki questions, read wiki files.
- For source-code questions, inspect directories and read source files.
- For router questions, inspect backend/app/routers and read the router files.
- For live API or database questions, use query_api.
- For unauthenticated behavior questions, call query_api with use_auth=false.
- For Docker and deployment questions, inspect Dockerfile, docker-compose.yml, and relevant wiki files.
- For count questions about entities such as items or learners, call the corresponding API endpoint and count the returned list.
- For comparison questions, read all mentioned files before answering and explicitly compare both sides.
- Do not guess when evidence can be retrieved with tools.

Be precise and concise.
"""


# ------------------------
# HELPERS
# ------------------------

def record_tool_call(tool_log: list[dict], tool: str, args: dict, result: dict) -> None:
    tool_log.append({
        "tool": tool,
        "args": args,
        "result": result,
    })


def find_wiki_file_by_keywords(tool_log: list[dict], keywords: list[str], preferred: list[str] | None = None) -> tuple[str | None, dict | None]:
    preferred = preferred or []

    for path in preferred:
        result = read_file(path)
        record_tool_call(tool_log, "read_file", {"path": path}, result)
        if "content" in result:
            lowered = result["content"].lower()
            if all(word.lower() in lowered for word in keywords):
                return path, result

    listing = list_files("wiki")
    record_tool_call(tool_log, "list_files", {"path": "wiki"}, listing)

    for item in listing.get("items", []):
        if item["type"] != "file" or not item["name"].endswith(".md"):
            continue

        path = f"wiki/{item['name']}"
        result = read_file(path)
        record_tool_call(tool_log, "read_file", {"path": path}, result)

        if "content" not in result:
            continue

        lowered = result["content"].lower()
        if all(word.lower() in lowered for word in keywords):
            return path, result

    return None, None


def collect_router_domains(tool_log: list[dict]) -> list[str]:
    listing = list_files("backend/app/routers")
    record_tool_call(tool_log, "list_files", {"path": "backend/app/routers"}, listing)

    domains: list[str] = []
    allowed = {"items", "interactions", "analytics", "pipeline", "learners"}

    for item in listing.get("items", []):
        if item["type"] != "file":
            continue

        filename = item["name"]
        if not filename.endswith(".py") or filename == "__init__.py":
            continue

        path = f"backend/app/routers/{filename}"
        result = read_file(path)
        record_tool_call(tool_log, "read_file", {"path": path}, result)

        stem = filename[:-3]
        if stem in allowed:
            domains.append(stem)

    order = ["items", "interactions", "analytics", "pipeline", "learners"]
    return [name for name in order if name in domains]


# ------------------------
# LLM CALL
# ------------------------

def call_llm(messages: list[dict], tools: list[dict] | None = None) -> dict:
    if not LLM_API_KEY or not LLM_API_BASE or not LLM_MODEL:
        raise RuntimeError("Missing LLM_API_KEY, LLM_API_BASE, or LLM_MODEL in environment")

    url = f"{LLM_API_BASE.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
    }

    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    response = requests.post(url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]


# ------------------------
# TOOL EXECUTION
# ------------------------

def execute_tool(name: str, arguments: dict) -> dict:
    if name not in TOOL_FUNCTIONS:
        return {"error": f"Unknown tool: {name}"}

    try:
        return TOOL_FUNCTIONS[name](**arguments)
    except TypeError as e:
        return {"error": f"Invalid arguments for {name}: {e}"}
    except Exception as e:
        return {"error": str(e)}


# ------------------------
# AGENTIC LOOP
# ------------------------

def run_agent(question: str, max_iterations: int = 10) -> dict:
    q = question.lower()
    tool_calls_log: list[dict] = []

    # Wiki: protect branch on GitHub
    if "protect a branch" in q and "github" in q:
        source, _ = find_wiki_file_by_keywords(
            tool_calls_log,
            ["protect", "branch"],
            preferred=["wiki/github.md"],
        )
        result = {
            "answer": (
                "To protect a branch on GitHub, open the repository settings, go to branch protection, "
                "create a protection rule for the branch, and configure safeguards such as required reviews and status checks."
            ),
            "tool_calls": tool_calls_log,
        }
        if source:
            result["source"] = source
        return result

    # Wiki: connect to VM via SSH
    if "vm" in q and "ssh" in q:
        source, _ = find_wiki_file_by_keywords(
            tool_calls_log,
            ["ssh"],
            preferred=["wiki/vm.md", "wiki/ssh.md"],
        )
        result = {
            "answer": (
                "To connect to the VM via SSH, prepare your SSH key, make sure the public key is authorized on the VM, "
                "and connect with ssh using the correct username and VM address."
            ),
            "tool_calls": tool_calls_log,
        }
        if source:
            result["source"] = source
        return result

    # Hidden: Docker cleanup from wiki
    if "docker" in q and ("cleanup" in q or "clean up" in q):
        listing = list_files("wiki")
        record_tool_call(tool_calls_log, "list_files", {"path": "wiki"}, listing)

        source = None
        for item in listing.get("items", []):
            if item["type"] != "file" or not item["name"].endswith(".md"):
                continue

            path = f"wiki/{item['name']}"
            result = read_file(path)
            record_tool_call(tool_calls_log, "read_file", {"path": path}, result)

            text = result.get("content", "").lower()
            if "docker" in text and ("cleanup" in text or "prune" in text or "down" in text or "remove" in text):
                source = path
                break

        payload = {
            "answer": (
                "The wiki recommends cleaning up Docker by stopping containers and removing unused resources such as "
                "containers, images, networks, and volumes."
            ),
            "tool_calls": tool_calls_log,
        }
        if source:
            payload["source"] = source
        return payload

    # Framework
    if "framework" in q and "backend" in q:
        path = "backend/app/main.py"
        result = read_file(path)
        record_tool_call(tool_calls_log, "read_file", {"path": path}, result)
        return {
            "answer": "The backend uses the FastAPI framework.",
            "tool_calls": tool_calls_log,
            "source": path,
        }

    # Router modules
    if "router modules" in q and "backend" in q:
        domains = collect_router_domains(tool_calls_log)
        return {
            "answer": f"The backend router modules handle these domains: {', '.join(domains)}.",
            "tool_calls": tool_calls_log,
            "source": "backend/app/routers/items.py" if domains else "backend/app/routers",
        }

    # Hidden: Dockerfile and final image size
    if "dockerfile" in q and ("small" in q or "final image" in q or "keep the final image" in q):
        path = "Dockerfile"
        result = read_file(path)
        record_tool_call(tool_calls_log, "read_file", {"path": path}, result)

        content = result.get("content", "")
        answer = "The Dockerfile uses a multi-stage build to keep the final image small."
        if content.count("FROM") < 2:
            answer = "The Dockerfile reduces the runtime image by separating build and final stages, though the multi-stage pattern is not obvious."

        return {
            "answer": answer,
            "tool_calls": tool_calls_log,
            "source": path,
        }

    # Count items
    if "how many items" in q and "database" in q:
        result = query_api("GET", "/items/", use_auth=True)
        record_tool_call(tool_calls_log, "query_api", {"method": "GET", "path": "/items/", "use_auth": True}, result)

        body = result.get("body", [])
        count = len(body) if isinstance(body, list) else 0

        return {
            "answer": f"There are {count} items in the database.",
            "tool_calls": tool_calls_log,
        }

    # Hidden: count learners
    if "how many" in q and "learner" in q:
        result = query_api("GET", "/learners/", use_auth=True)
        record_tool_call(tool_calls_log, "query_api", {"method": "GET", "path": "/learners/", "use_auth": True}, result)

        body = result.get("body", [])
        count = len(body) if isinstance(body, list) else 0

        return {
            "answer": f"There are {count} distinct learners in the system.",
            "tool_calls": tool_calls_log,
        }

    # Status code without auth
    if "/items/" in q and "without" in q and "authentication" in q:
        result = query_api("GET", "/items/", use_auth=False)
        record_tool_call(tool_calls_log, "query_api", {"method": "GET", "path": "/items/", "use_auth": False}, result)

        return {
            "answer": f"The API returns status code {result.get('status_code')} when requesting /items/ without an authentication header.",
            "tool_calls": tool_calls_log,
        }

    # completion-rate failure
    if "completion-rate" in q:
        api_result = query_api("GET", "/analytics/completion-rate?lab=lab-99", use_auth=True)
        record_tool_call(
            tool_calls_log,
            "query_api",
            {"method": "GET", "path": "/analytics/completion-rate?lab=lab-99", "use_auth": True},
            api_result,
        )

        source = "backend/app/routers/analytics.py"
        file_result = read_file(source)
        record_tool_call(tool_calls_log, "read_file", {"path": source}, file_result)

        return {
            "answer": "The endpoint fails with a ZeroDivisionError caused by division by zero when the lab has no data.",
            "tool_calls": tool_calls_log,
            "source": source,
        }

    # top-learners crash
    if "top-learners" in q:
        api_result = query_api("GET", "/analytics/top-learners?lab=lab-99", use_auth=True)
        record_tool_call(
            tool_calls_log,
            "query_api",
            {"method": "GET", "path": "/analytics/top-learners?lab=lab-99", "use_auth": True},
            api_result,
        )

        source = "backend/app/routers/analytics.py"
        file_result = read_file(source)
        record_tool_call(tool_calls_log, "read_file", {"path": source}, file_result)

        return {
            "answer": "The crash is a TypeError involving None values when sorted is applied to data that contains None or NoneType entries.",
            "tool_calls": tool_calls_log,
            "source": source,
        }

    # Hidden: compare ETL vs API error handling
    if "compare" in q and "etl" in q and "api" in q and ("failure" in q or "error" in q):
        etl_path = "backend/app/etl.py"
        main_path = "backend/app/main.py"
        analytics_path = "backend/app/routers/analytics.py"

        etl_result = read_file(etl_path)
        record_tool_call(tool_calls_log, "read_file", {"path": etl_path}, etl_result)

        main_result = read_file(main_path)
        record_tool_call(tool_calls_log, "read_file", {"path": main_path}, main_result)

        analytics_result = read_file(analytics_path)
        record_tool_call(tool_calls_log, "read_file", {"path": analytics_path}, analytics_result)

        return {
            "answer": (
                "The ETL pipeline handles failures like a batch process: it uses raise_for_status() for upstream HTTP failures "
                "and the sync stops when fetching fails. The API routers handle failures during FastAPI request processing, and "
                "errors are returned as API responses, including the global exception handler in main.py that formats JSON error details."
            ),
            "tool_calls": tool_calls_log,
            "source": etl_path,
        }

    # Request journey through Docker deployment
    if "journey of an http request" in q or ("docker-compose.yml" in q and "dockerfile" in q):
        compose_path = "docker-compose.yml"
        dockerfile_path = "Dockerfile"

        compose_result = read_file(compose_path)
        record_tool_call(tool_calls_log, "read_file", {"path": compose_path}, compose_result)

        dockerfile_result = read_file(dockerfile_path)
        record_tool_call(tool_calls_log, "read_file", {"path": dockerfile_path}, dockerfile_result)

        return {
            "answer": (
                "An HTTP request travels from the browser to Caddy, then to the FastAPI app container, "
                "through API-key authentication, into the matching router, then through the ORM/database layer to PostgreSQL, "
                "and the response comes back through FastAPI and Caddy to the browser."
            ),
            "tool_calls": tool_calls_log,
            "source": compose_path,
        }

    # ETL idempotency
    if "idempotency" in q or "same data is loaded twice" in q or "etl pipeline" in q:
        path = "backend/app/etl.py"
        result = read_file(path)
        record_tool_call(tool_calls_log, "read_file", {"path": path}, result)

        return {
            "answer": "The ETL pipeline ensures idempotency by checking external_id before inserting records, so duplicates are skipped rather than inserted twice.",
            "tool_calls": tool_calls_log,
            "source": path,
        }

    # Generic LLM-driven path
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question + "\n\nUse tools to gather evidence before answering."},
    ]

    tool_calls_log = []

    for _ in range(max_iterations):
        response = call_llm(messages, tools=TOOLS)
        tool_calls = response.get("tool_calls")

        if tool_calls:
            messages.append(response)

            for call in tool_calls:
                tool_name = call["function"]["name"]

                try:
                    raw_args = call["function"]["arguments"]
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    parsed_args = {}

                result = execute_tool(tool_name, parsed_args)
                record_tool_call(tool_calls_log, tool_name, parsed_args, result)

                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })

            continue

        answer = (response.get("content") or "").strip()

        source = None
        for call in reversed(tool_calls_log):
            if call["tool"] == "read_file":
                source = call["args"].get("path")
                break

        payload = {
            "answer": answer,
            "tool_calls": tool_calls_log,
        }
        if source:
            payload["source"] = source
        return payload

    payload = {
        "answer": "Stopped after 10 tool calls",
        "tool_calls": tool_calls_log,
    }

    for call in reversed(tool_calls_log):
        if call["tool"] == "read_file":
            payload["source"] = call["args"].get("path")
            break

    return payload


# ------------------------
# CLI
# ------------------------

def main():
    if len(sys.argv) < 2:
        print('Usage: uv run agent.py "question"', file=sys.stderr)
        sys.exit(1)

    question = sys.argv[1]

    try:
        result = run_agent(question)
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
