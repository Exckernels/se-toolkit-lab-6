from __future__ import annotations

import io
import json
import urllib.error

import agent


class FakeHTTPResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = json.dumps(payload).encode("utf-8")
        self._status_code = status_code

    def read(self) -> bytes:
        return self._payload

    def getcode(self) -> int:
        return self._status_code

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_framework_question_uses_read_file(monkeypatch) -> None:
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "backend/app/main.py"}),
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answer": "The backend uses FastAPI.",
                                "source": "backend/app/main.py",
                            }
                        )
                    }
                }
            ]
        },
    ]

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_BASE", "http://example.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        assert timeout == agent.DEFAULT_TIMEOUT_SECONDS
        assert request.full_url == "http://example.test/v1/chat/completions"
        assert responses, "No mocked LLM responses left"
        return FakeHTTPResponse(responses.pop(0))

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)

    result = agent.answer_question("What Python web framework does this project's backend use?")

    assert result["answer"] == "The backend uses FastAPI."
    assert result["source"] == "backend/app/main.py"
    assert any(tool_call["tool"] == "read_file" for tool_call in result["tool_calls"])
    assert any(
        tool_call["args"] == {"path": "backend/app/main.py"}
        for tool_call in result["tool_calls"]
        if tool_call["tool"] == "read_file"
    )


def test_item_count_question_uses_query_api(monkeypatch) -> None:
    llm_responses = [
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "query_api",
                                    "arguments": json.dumps({"method": "GET", "path": "/items/"}),
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answer": "There are 3 items in the database.",
                                "source": "/items/",
                            }
                        )
                    }
                }
            ]
        },
    ]
    api_calls: list[tuple[str, str | None]] = []

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_BASE", "http://example.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LMS_API_KEY", "backend-secret")
    monkeypatch.setenv("AGENT_API_BASE_URL", "http://backend.test")

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        assert timeout == agent.DEFAULT_TIMEOUT_SECONDS

        if request.full_url == "http://example.test/v1/chat/completions":
            assert llm_responses, "No mocked LLM responses left"
            return FakeHTTPResponse(llm_responses.pop(0))

        if request.full_url == "http://backend.test/items/":
            auth_header = request.headers.get("Authorization")
            api_calls.append((request.full_url, auth_header))
            if auth_header == "Bearer backend-secret":
                return FakeHTTPResponse(
                    [
                        {"id": 1, "title": "Item 1"},
                        {"id": 2, "title": "Item 2"},
                        {"id": 3, "title": "Item 3"},
                    ]
                )

            raise urllib.error.HTTPError(
                url=request.full_url,
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=io.BytesIO(json.dumps({"detail": "Missing credentials"}).encode("utf-8")),
            )

        raise AssertionError(f"Unexpected URL: {request.full_url}")

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)

    result = agent.answer_question("How many items are currently stored in the database?")

    assert result["answer"] == "There are 3 items in the database."
    assert any(tool_call["tool"] == "query_api" for tool_call in result["tool_calls"])
    assert any(
        tool_call["args"] == {"method": "GET", "path": "/items/"}
        for tool_call in result["tool_calls"]
        if tool_call["tool"] == "query_api"
    )
    assert api_calls
    assert any(auth == "Bearer backend-secret" for _, auth in api_calls)
    assert any(auth is None for _, auth in api_calls)
