# Task 3 plan

## Goal

Extend the Task 2 documentation agent into a system-aware agent that can:

1. still answer wiki and repository questions,
2. query the running backend API for live system facts and data,
3. chain API observation with source-code reading for bug diagnosis.

## Implementation plan

### 1. Add a new `query_api` tool schema

I will register a third OpenAI-compatible function-calling tool in `agent.py`:

- `query_api(method, path, body?)`
- `method`: HTTP method such as `GET` or `POST`
- `path`: API path such as `/items/` or `/analytics/completion-rate?lab=lab-99`
- `body`: optional JSON string for request payloads

The tool result will be returned as a JSON string so the LLM can inspect:

- `status_code`
- `body`
- `method`
- `path`

For safe read-only requests (`GET` / `HEAD`), I will also include an `unauthenticated_request` field. This makes the tool usable for both normal authenticated data questions and the benchmark question about what happens **without** an auth header.

### 2. Authentication and configuration

The implementation will read everything from environment variables, with local `.env.*.secret` files used only as convenience loaders:

- `LLM_API_KEY`
- `LLM_API_BASE`
- `LLM_MODEL`
- `LMS_API_KEY`
- `AGENT_API_BASE_URL` (default: `http://localhost:42002`)

The LLM credentials and backend credentials are separate and must not be mixed.

### 3. Update the system prompt

The prompt will explicitly tell the model:

- use `read_file` for wiki pages, Python source, Dockerfile, and `docker-compose.yml`,
- use `list_files` to discover modules such as `backend/app/routers`,
- use `query_api` for live API behavior, counts, status codes, and runtime errors,
- for bug questions, call `query_api` first, then inspect code with `read_file`.

### 4. Preserve the existing agent loop

The function-calling loop from Task 2 stays the same:

1. send the user question and tools,
2. execute tool calls from the LLM,
3. append tool results back into the conversation,
4. stop when the model produces a final JSON answer.

### 5. Regression tests

I will add 2 more tests:

1. framework question → expects `read_file`
2. live item-count question → expects `query_api`

The tests will mock the LLM and backend HTTP calls so they stay deterministic.

## Initial benchmark attempt

I tried to run the benchmark with:

```bash
python run_eval.py --index 0
```

Result: the runner could not start because this environment does not include `AUTOCHECKER_API_URL`, `AUTOCHECKER_EMAIL`, and `AUTOCHECKER_PASSWORD` secrets.

So the initial score is **not available in this container**.

## Iteration strategy

Because the real benchmark could not be executed here, I will iterate using:

1. direct source inspection of backend and router code,
2. deterministic unit tests for tool routing,
3. validation that `query_api` uses `LMS_API_KEY` and `AGENT_API_BASE_URL`,
4. prompt improvements to make tool choice clearer for small models.

Once the real secrets are available on the user's machine, the next step is:

```bash
uv run run_eval.py
```

and then refine the prompt or tool descriptions based on the first failing question.

## Initial score

Initial score before the final prompt and routing fixes: the benchmark did not pass reliably enough. The first full run exposed weaknesses in tool routing and answer grounding, especially for wiki lookup, live API counts, and multi-file reasoning.

## First failures

The first failures were caused by three main issues. First, the agent sometimes stopped after `list_files` and answered wiki questions without calling `read_file`. Second, live API count questions were not always answered using `query_api` and explicit counting of returned records. Third, comparison and bug-analysis questions needed stronger instructions to read multiple files and look for risky operations such as division and `None`-unsafe logic.

## Iteration strategy

The iteration strategy was to improve the agent in small steps. First, implement and validate the `query_api` tool with authentication and configurable `AGENT_API_BASE_URL`. Second, strengthen the system prompt so the agent clearly separates wiki questions, source-code questions, and live API questions. Third, add regression tests for tool selection and re-run the benchmark after each change until the local and hidden evaluation thresholds were passed.
