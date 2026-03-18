# Task 3 Plan

## Implementation plan

I will extend the Task 2 repository agent with a third tool called `query_api`. The tool schema will accept three parameters: `method`, `path`, and optional `body`. The implementation will live in `agent.py` next to `list_files` and `read_file`, and the same function-calling loop will continue to drive the agent.

The new tool must authenticate against the running backend. To do that, `query_api` will read `LMS_API_KEY` from environment variables and send it in the `Authorization` header as a Bearer token. It will also read `AGENT_API_BASE_URL` from environment variables and fall back to `http://localhost:42002` when the variable is missing.

I will also keep all model configuration externalized. The agent will read `LLM_API_KEY`, `LLM_API_BASE`, and `LLM_MODEL` from environment variables instead of hardcoding values.

The system prompt will be updated so the model knows when to use wiki tools, when to read source code, and when to query the live API. Wiki and process questions should use `read_file` on files in `wiki/`. Static implementation questions should use `read_file` on source files, Docker files, or config files. Current-data questions, authentication questions, and runtime endpoint errors should use `query_api`.

## Initial score

Initial score before the final routing and prompt fixes: the benchmark did not pass reliably enough. The early run showed that the agent needed better tool selection and more explicit answer grounding.

## First failures

The first failures came from three recurring problems. First, wiki questions were sometimes answered too early after directory discovery without reading the relevant file. Second, count questions were not always handled with `query_api` plus explicit counting of returned records. Third, bug-diagnosis and comparison questions needed stronger instructions to combine runtime evidence from the API with source-code inspection.

## Iteration strategy

The iteration strategy was to improve the agent in three passes. First, implement and validate the `query_api` tool schema, authentication, and `AGENT_API_BASE_URL` support. Second, strengthen the system prompt so the agent clearly separates wiki questions, source-code questions, and live API questions. Third, add targeted regression tests and iterate on benchmark failures until both the local and hidden evaluation thresholds were passed.
