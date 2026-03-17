from __future__ import annotations

import json

import agent


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_agent_main_outputs_required_json_fields(monkeypatch, capsys) -> None:
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "answer": "REST stands for Representational State Transfer.",
                                "source": "wiki/rest-api.md",
                            }
                        )
                    }
                }
            ]
        }
    ]

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        assert timeout == agent.DEFAULT_TIMEOUT_SECONDS
        payload = json.loads(request.data.decode("utf-8"))
        assert payload["tools"]
        return FakeHTTPResponse(responses.pop(0))

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_BASE", "http://example.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)

    exit_code = agent.main(["agent.py", "What does REST stand for?"])

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err

    payload = json.loads(captured.out)
    assert payload["answer"] == "REST stands for Representational State Transfer."
    assert payload["source"] == "wiki/rest-api.md"
    assert payload["tool_calls"] == []
