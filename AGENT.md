# Documentation Agent Architecture

## Overview

`agent.py` implements a CLI documentation agent for this repository.

Compared with Task 1, the agent now has repository tools and an agentic loop. Instead of answering from the model alone, it can inspect the local wiki and repository files before producing the final answer.

## Entry point

Run the agent with:

```bash
uv run agent.py "How do you resolve a merge conflict?"
```

The program prints exactly one JSON object to stdout:

```json
{
  "answer": "Choose which version to keep (or combine it), remove the conflict markers, and commit the result.",
  "source": "wiki/git.md#merge-conflict",
  "tool_calls": [
    {"tool": "list_files", "args": {"path": "wiki"}, "result": "..."},
    {"tool": "read_file", "args": {"path": "wiki/git.md"}, "result": "..."}
  ]
}
```

## Tools

The agent exposes two OpenAI-compatible function-calling tools.

### `list_files`

- **Purpose:** discover candidate files and directories before reading them
- **Parameters:** `path` (relative path from repository root)
- **Returns:** newline-separated listing of entries

Example:

```text
list_files({"path": "wiki"})
```

### `read_file`

- **Purpose:** inspect a repository file, usually after discovering it with `list_files`
- **Parameters:** `path` (relative path from repository root)
- **Returns:** full file contents as UTF-8 text

Example:

```text
read_file({"path": "wiki/git.md"})
```

## Path security

Both tools are sandboxed to the repository root.

Implementation strategy:

1. resolve the requested relative path against `ROOT_DIR`,
2. normalize it with `.resolve()`,
3. verify that the resolved path still belongs to `ROOT_DIR`,
4. return an error string if the path escapes the project.

This prevents `../` traversal and absolute-path escapes.

## Agentic loop

The agent follows this loop:

1. send system prompt + user question + tool schemas to the LLM,
2. if the LLM returns `tool_calls`, execute them,
3. append each result back as a `tool` message,
4. ask the LLM again,
5. stop when the LLM returns a normal message without `tool_calls`,
6. if 10 tool calls are reached, stop and return the best answer gathered so far.

Every executed tool call is also recorded for the final `tool_calls` field.

## System prompt strategy

The system prompt tells the model to:

- behave like a documentation agent for this repository,
- use `list_files` first to discover relevant files, especially under `wiki/`,
- use `read_file` to inspect the best matching documentation file,
- answer with a final JSON object containing `answer` and `source`,
- prefer a source like `wiki/file.md#section-anchor`,
- avoid inventing files or anchors.

This prompt keeps the model grounded in the repository instead of free-answering from memory.

## LLM configuration

The agent reads configuration from `.env.agent.secret` and environment variables.

Required variables:

- `LLM_API_KEY`
- `LLM_API_BASE`
- `LLM_MODEL`

The API is expected to be OpenAI-compatible and support `/chat/completions` with function calling.

## Tests

Regression tests are in `tests/`.

They mock the LLM API responses so the tool-calling behavior is deterministic and does not require a live remote model.

## Final architecture and lessons learned

The final agent uses three tools: `list_files`, `read_file`, and `query_api`.

`list_files` is used to discover candidate files and wiki pages. `read_file` is used for source-code questions, configuration analysis, Docker and routing questions, and detailed wiki reading. `query_api` is used for live API behavior, endpoint status codes, authenticated and unauthenticated requests, and current data questions such as counts of items or learners.

The `query_api` tool reads `AGENT_API_BASE_URL` from the environment. If the variable is not set, it falls back to the default local API base URL. Authentication is handled with `LMS_API_KEY`, which is sent as a Bearer token in the `Authorization` header for authenticated API requests. The language model configuration is controlled by `LLM_API_KEY`, `LLM_API_BASE`, and `LLM_MODEL`.

The final architecture separates three kinds of evidence: wiki/process evidence, source-code evidence, and live-runtime evidence. Wiki questions are answered by first discovering files and then reading the relevant wiki page. Static implementation questions are answered by reading source files. Runtime and data questions are answered through `query_api`, sometimes followed by source inspection when a bug explanation is needed.

Lessons learned: the benchmark was most sensitive to tool routing. The agent needed explicit instructions for when to use wiki lookup, when to inspect source code, and when to query the live API. It also helped to make the agent more explicit for count questions, bug-finding questions, and multi-file comparison questions.

Final eval score: local questions passed (>= 80%) and hidden eval passed (>= 80%).
