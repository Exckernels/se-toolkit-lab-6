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


def install_fake_llm(monkeypatch, responses: list[dict[str, object]]) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_API_BASE", "http://example.test/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        assert timeout == agent.DEFAULT_TIMEOUT_SECONDS
        assert responses, "No mocked LLM responses left"
        return FakeHTTPResponse(responses.pop(0))

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)


def test_merge_conflict_question_uses_read_file_and_returns_git_source(monkeypatch) -> None:
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
                                    "name": "list_files",
                                    "arguments": json.dumps({"path": "wiki"}),
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
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "wiki/git.md"}),
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
                                "answer": "Choose which version to keep or combine, remove all conflict markers, and commit the result.",
                                "source": "wiki/git.md#merge-conflict",
                            }
                        )
                    }
                }
            ]
        },
    ]
    install_fake_llm(monkeypatch, responses)

    result = agent.answer_question("How do you resolve a merge conflict?")

    assert result["source"] == "wiki/git.md#merge-conflict"
    assert any(tool_call["tool"] == "read_file" for tool_call in result["tool_calls"])
    assert any(
        tool_call["args"] == {"path": "wiki/git.md"}
        for tool_call in result["tool_calls"]
        if tool_call["tool"] == "read_file"
    )


def test_wiki_listing_question_uses_list_files(monkeypatch) -> None:
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
                                    "name": "list_files",
                                    "arguments": json.dumps({"path": "wiki"}),
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
                                "answer": "The wiki contains many Markdown files such as git.md, git-vscode.md, rest-api.md, and backend.md.",
                                "source": "wiki",
                            }
                        )
                    }
                }
            ]
        },
    ]
    install_fake_llm(monkeypatch, responses)

    result = agent.answer_question("What files are in the wiki?")

    assert result["tool_calls"]
    assert result["tool_calls"][0]["tool"] == "list_files"
    assert result["tool_calls"][0]["args"] == {"path": "wiki"}
    assert "git.md" in result["tool_calls"][0]["result"]
