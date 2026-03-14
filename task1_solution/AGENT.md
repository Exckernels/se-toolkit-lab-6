# AGENT.md

## Overview

This project contains a minimal CLI agent for Task 1.

The agent:

1. reads the user question from the first command-line argument,
2. loads LLM settings from `.env.agent.secret`,
3. sends the request to an OpenAI-compatible `/chat/completions` endpoint,
4. prints a single JSON object to stdout.

## LLM provider

Recommended configuration for this repository:

- Provider: Qwen Code API proxy on the remote VM
- API base: `http://127.0.0.1:42005/v1` (or your actual proxy port)
- Model: `qwen3-coder-plus`

Required variables in `.env.agent.secret`:

```env
LLM_API_KEY=my-secret-qwen-key
LLM_API_BASE=http://127.0.0.1:42005/v1
LLM_MODEL=qwen3-coder-plus
```

## How the agent works

- `agent.py` loads `.env.agent.secret` from the project root.
- It sends a chat-completions request with a minimal system prompt.
- It extracts the assistant text from `choices[0].message.content`.
- It prints JSON in the required Task 1 format:

```json
{"answer": "...", "tool_calls": []}
```

All diagnostic output goes to stderr.

## Run the agent

```bash
uv run agent.py "What does REST stand for?"
```

## Expected output format

```json
{"answer": "Representational State Transfer.", "tool_calls": []}
```

## Testing

Run the regression test with:

```bash
uv run pytest -q
```
