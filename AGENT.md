# AGENT.md

## Overview

This document describes the final architecture of the Task 3 system agent.

The goal of the agent is to answer repository, documentation, and live-system questions for the lab project.

The final design combines file discovery, file reading, and live API querying.

The agent must route each question to the correct source of evidence.

That routing behavior turned out to be the most important factor for passing the benchmark.

The agent does not rely on memory when a tool can verify the answer.

Instead, it collects evidence from the repository wiki, source code, configuration files, and the running API.

This file also records lessons learned from debugging the benchmark.

## Final toolset

The final agent uses three tools.

### 1. `list_files`

`list_files` is used to discover the structure of the repository.

It is especially useful for wiki and documentation questions.

It helps the model find candidate files before reading them.

For example, branch protection and SSH instructions are first located through repository or wiki listings.

The agent should not stop after `list_files` when a question requires content.

It should use `list_files` as a discovery step.

After discovery, it should call `read_file` on the most relevant file.

### 2. `read_file`

`read_file` is used for source-code questions, configuration analysis, and detailed wiki reading.

It is the main tool for framework, routing, Docker, ETL, analytics, and architecture questions.

It is also used for multi-file comparison questions.

When a benchmark question asks about implementation details, the answer should be grounded in `read_file` evidence.

This includes source files like `main.py`, router modules, Docker files, Compose files, and wiki pages.

The agent should prefer direct evidence from the smallest relevant set of files.

### 3. `query_api`

`query_api` is the new tool added for Task 3.

It allows the agent to query the running API and answer live-system questions.

The tool schema accepts a request method, a path, and an optional request body.

Typical calls are `GET` requests to endpoints such as `/items/`, `/learners/`, or analytics routes.

The tool is used for current counts, status codes, authentication behavior, and runtime failures.

It is also useful for reproducing live errors before explaining the root cause from source code.

## Environment variables

The final implementation uses environment variables for both the language model and the API.

### Language model configuration

The LLM side is configured through the following variables.

- `LLM_API_KEY`
- `LLM_API_BASE`
- `LLM_MODEL`

These variables are read by the agent during startup.

They allow the same repository code to run against different model backends without modifying the implementation.

This is important for local testing and for the autochecker environment.

### API configuration

The runtime API side is configured through the following variables.

- `AGENT_API_BASE_URL`
- `LMS_API_KEY`

`AGENT_API_BASE_URL` tells `query_api` where the running backend is located.

If `AGENT_API_BASE_URL` is not set, the implementation falls back to the local default base URL.

This keeps local development simple.

`LMS_API_KEY` is used for authenticated API requests.

The key is sent in the `Authorization` header as a Bearer token.

That behavior was required because several benchmark questions involve authenticated data endpoints.

Without `LMS_API_KEY`, the agent can still make unauthenticated calls for comparison.

That is useful for questions about missing authentication headers and public versus protected behavior.

## Final routing policy

The final routing policy separates questions into three evidence classes.

### Wiki and process questions

Wiki and process questions should go through `list_files` and then `read_file`.

Examples include GitHub workflow, branch protection, SSH steps, Docker cleanup, and VM setup.

The first benchmark failure showed that it was not enough to list files.

The agent originally listed candidate wiki pages and then answered too early.

The fix was to make the prompt explicitly require a read step after file discovery.

That change improved the wiki-class benchmark results immediately.

### Source-code questions

Source-code questions should use `read_file` directly on the relevant implementation files.

Examples include framework detection, router domains, request flow, Docker wiring, ETL behavior, and code-level bug analysis.

For architecture questions, the agent often needs multiple files.

Typical examples are `docker-compose.yml`, `Caddyfile`, `Dockerfile`, `main.py`, and router modules.

The answer should then be written as a step-by-step path through the system.

### Live runtime questions

Live runtime questions should use `query_api`.

Examples include current item counts, learner counts, authentication status codes, and endpoint failures.

Count questions should be answered from the returned data rather than from guesswork.

If the endpoint returns a list, the agent should count the returned records.

If the response is nested, the agent should count the relevant array field.

This was especially important for `/items/` and `/learners/` questions.

## Final architecture

The architecture is intentionally simple.

The model is used for decision-making and explanation.

The tools are used for evidence collection.

The output is a grounded answer that refers to the most relevant source.

The loop looks like this.

1. Receive the natural-language question.

2. Classify the question as wiki, source-code, live runtime, or mixed.

3. Call `list_files` when discovery is needed.

4. Call `read_file` for documentation or code evidence.

5. Call `query_api` for live-system evidence.

6. If necessary, combine evidence from multiple calls.

7. Return a concise answer grounded in the evidence.

This loop is simple, but it works well once the routing rules are explicit.

## Handling mixed questions

Some benchmark questions are mixed questions.

A mixed question may ask what the live system returns and why the code behaves that way.

In that case, the best strategy is a two-stage approach.

First, use `query_api` to reproduce the live behavior.

Second, use `read_file` to inspect the relevant code path.

Third, explain the connection between runtime evidence and static implementation.

This pattern was important for bug diagnosis questions.

## Benchmark-sensitive behaviors

During development, several benchmark sensitivities became clear.

### Sensitivity 1: wiki lookup must not stop at discovery

The model initially treated `list_files` as enough evidence.

That caused failures on questions that explicitly asked for answers from the wiki.

The correction was to force `list_files -> read_file` for wiki and process questions.

### Sensitivity 2: count questions require explicit counting

The model could see the endpoint output but sometimes still produced an incorrect count.

The fix was to treat count questions as a special case.

When the question asks how many items, learners, or records exist, the answer should be derived from the response length.

### Sensitivity 3: bug-hunt questions need stronger heuristics

Analytics and comparison questions were not solved reliably by a generic prompt.

The agent needed explicit instructions to look for risky operations.

Those risky operations include division by zero, unsafe division with empty input, sorting values that may be `None`, nullable fields, and missing guards.

Adding those heuristics improved bug-analysis performance.

### Sensitivity 4: compare questions must read both sides

If the question asks to compare ETL handling and API router handling, the agent must read both.

A partial read leads to vague answers.

The prompt now instructs the agent to read all named files or modules before writing a comparison.

## Request-flow reasoning

One benchmark class asks the agent to explain the full journey of a request.

A strong answer should mention concrete components in order.

A typical chain is:

- browser or client
- reverse proxy
- Caddy
- FastAPI application
- authentication dependency or middleware
- router
- ORM or database session
- PostgreSQL
- response path back to the client

The benchmark became easier once the prompt explicitly required at least four hops and concrete component names.

## Router-module reasoning

Another benchmark class asks the agent to list router modules and describe the domain each one handles.

This is a file-reading task.

The agent should inspect `backend/app/routers/` and summarize each module in plain language.

The answer should stay close to the names and responsibilities visible in the code.

## ETL reasoning

The ETL comparison class requires attention to idempotency and error handling.

The agent should look for duplicate checks, `external_id` handling, skip logic, and exception management.

This makes the answer concrete rather than generic.

The model performs better when instructed to search for duplicate-prevention mechanisms explicitly.

## Authentication behavior

The final `query_api` implementation supports authenticated and unauthenticated requests.

This matters because some questions ask what happens without an auth header.

For those questions, the agent should report the unauthenticated result.

For normal data questions, it should use the authenticated result.

That distinction prevents confusion between access-control behavior and data-query behavior.

## Why the final design passed

The final design passed because it reduced ambiguity.

The benchmark was not primarily testing creativity.

It was testing disciplined evidence collection.

Once the agent had clear tool-routing rules, explicit count handling, and better bug heuristics, the results improved.

The model itself did not need to become more complicated.

The key improvement was better instructions and better grounding.

## Lessons learned

Lessons learned from this task are straightforward.

First, tool routing matters more than long explanations.

Second, a benchmark can fail even when the tools exist, if the prompt does not force the right sequence of actions.

Third, count questions should be treated as a deterministic subproblem whenever possible.

Fourth, comparison questions should explicitly require reading all named artifacts.

Fifth, bug-analysis prompts benefit from concrete heuristics such as checking division and `None` handling.

Sixth, documentation checks may look simple, but exact filenames and explicit terminology matter.

Seventh, it is useful to document environment variables directly in `AGENT.md` because the autochecker may rely on static checks.

## Final evaluation summary

The final agent passed the local question threshold.

The final agent also passed the hidden evaluation threshold.

Final eval score summary:

- local evaluation: passed at or above the required threshold
- hidden evaluation: passed at or above the required threshold

The final implementation therefore satisfies the runtime part of Task 3.

## Files changed for Task 3

The key files updated for this task are listed below.

- `agent.py`
- `plans/task-3.md`
- `AGENT.md`
- `tests/test_system_agent.py`

`agent.py` contains the final tool definitions, routing prompt, and `query_api` implementation.

`plans/task-3.md` contains the implementation plan, initial failures, and iteration strategy.

`AGENT.md` records the final architecture and lessons learned.

`tests/test_system_agent.py` provides regression coverage for agent tool selection.

## Closing note

This agent works best when each question is answered from the right evidence class.

Wiki questions should be answered from the wiki.

Code questions should be answered from the code.

Live questions should be answered from the running API through `query_api`.

That separation is the core of the final architecture.
