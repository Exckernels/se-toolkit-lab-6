# Task 1 plan

## Goal

Build a minimal CLI agent that accepts a question, sends it to an LLM, and prints one JSON object to stdout.

## LLM choice

I will use the Qwen Code API proxy deployed on the remote VM.

- Provider: Qwen Code API proxy
- API type: OpenAI-compatible chat completions API
- Model: `qwen3-coder-plus`

## Configuration

The agent will read its LLM settings from `.env.agent.secret`.

Required variables:

- `LLM_API_KEY`
- `LLM_API_BASE`
- `LLM_MODEL`

This keeps secrets out of the source code.

## Agent structure

`agent.py` will be split into a few simple steps:

1. read the first CLI argument as the user question,
2. load `.env.agent.secret`,
3. validate that the required environment variables exist,
4. call `POST /chat/completions`,
5. extract the assistant answer,
6. print valid JSON with `answer` and `tool_calls`.

## Output contract

The program must print exactly one JSON object to stdout:

```json
{"answer": "...", "tool_calls": []}
```

No debug text should be printed to stdout.

## Testing plan

I will add one regression test that:

1. runs `agent.py` as a subprocess,
2. checks that the process exits successfully,
3. parses stdout as JSON,
4. verifies that `answer` exists,
5. verifies that `tool_calls` exists and is a list.
