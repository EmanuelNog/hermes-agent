"""Regression: memory-provider tool schemas in Anthropic shape (input_schema)
leak into OpenAI-format requests and strict providers (opencode-go, DeepSeek)
reject the WHOLE request — every model call 400s for the session."""

from __future__ import annotations

from agent.memory_manager import normalize_tool_schema


def test_input_schema_mapped_to_parameters() -> None:
    s = normalize_tool_schema({
        "name": "wikisearch",
        "description": "search the wiki",
        "input_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}, "n": {"type": "integer"}},
            "required": ["q"],
        },
    })
    assert s is not None
    assert "parameters" in s, "input_schema must be mapped to parameters"
    assert "input_schema" not in s
    assert s["parameters"]["type"] == "object"
    assert s["parameters"]["required"] == ["q"]


def test_wrapped_openai_form_still_unwraps() -> None:
    s = normalize_tool_schema({
        "type": "function",
        "function": {
            "name": "t",
            "description": "d",
            "parameters": {"type": "object", "properties": {}},
        },
    })
    assert s is not None
    assert s["name"] == "t"
    assert s["parameters"] == {"type": "object", "properties": {}}


def test_nameless_schema_is_none() -> None:
    assert normalize_tool_schema({"description": "d", "parameters": {}}) is None
    assert normalize_tool_schema("not-a-dict") is None
