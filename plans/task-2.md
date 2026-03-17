# Task 2 plan

## Goal

Upgrade the Task 1 CLI into a documentation agent that can inspect the repository wiki with tools and answer with both an `answer` and a `source`.

## Tool schemas

I will define two OpenAI-compatible function-calling tools in `agent.py`:

1. `list_files(path)`
   - input: relative directory path
   - output: newline-separated directory listing
2. `read_file(path)`
   - input: relative file path
   - output: UTF-8 file contents

Both tools will be included in the chat-completions request under `tools`, with `tool_choice="auto"`.

## Agentic loop

The loop will work like this:

1. send the system prompt, user question, and tool schemas to the LLM,
2. if the assistant returns `tool_calls`, execute them one by one,
3. append each tool result back as a `tool` role message,
4. call the LLM again,
5. stop when the assistant returns a normal text answer with no `tool_calls`,
6. if the total number of executed tool calls reaches 10, stop and return the best answer collected so far.

I will keep a `tool_calls` log for the final JSON output. Each log entry will store:

- `tool`
- `args`
- `result`

## Final answer format

The system prompt will instruct the LLM to return a JSON object in its final text response:

```json
{"answer": "...", "source": "wiki/file.md#section-anchor"}
```

The Python code will parse that JSON. If parsing fails, it will fall back to plain text plus the most recent file path used as the source.

## Path security

Both tools must stay inside the repository root.

I will:

1. resolve every requested path against `ROOT_DIR`,
2. call `.resolve()` on the candidate path,
3. verify that the resolved path is still inside `ROOT_DIR` using `relative_to`,
4. return an error string if the path escapes the repository root,
5. reject wrong path types (directory passed to `read_file`, file passed to `list_files`).

This blocks `../` traversal and absolute-path escapes.

## Testing plan

I will add deterministic regression tests that mock the LLM HTTP responses instead of calling a real API.

Planned tests:

1. merge-conflict question:
   - the mocked LLM asks for `list_files` and `read_file`,
   - the final output must include a `read_file` tool call,
   - the `source` must point to the git documentation section.
2. wiki listing question:
   - the mocked LLM asks for `list_files`,
   - the final output must include a `list_files` tool call.
3. CLI contract smoke test:
   - `main()` prints valid JSON with `answer`, `source`, and `tool_calls`.
