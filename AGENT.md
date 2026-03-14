# Agent Architecture

## Entry point
`agent.py` is the CLI entry point. It accepts a question as input and returns a JSON response.

## Components
- question parsing
- LLM client initialization from environment variables
- response generation
- JSON formatting

## Configuration
The agent reads configuration from environment variables.

## Testing
Regression tests are located in the repository test files.
