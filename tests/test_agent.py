import asyncio
from types import SimpleNamespace

import pytest
from model_library.base import TextInput
from model_library.base.input import RawResponse

import proof_bench.agent as agent_module
from proof_bench.agent import _before_query, _is_empty_raw_response


def test_is_empty_raw_response_detects_assistant_without_content_or_tools():
    item = RawResponse(response=SimpleNamespace(role="assistant", content=None, tool_calls=None))

    assert _is_empty_raw_response(item)


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(role="assistant", content="text", tool_calls=None),
        SimpleNamespace(role="assistant", content=None, tool_calls=[{"name": "submit_proof"}]),
        SimpleNamespace(role="user", content=None, tool_calls=None),
    ],
)
def test_is_empty_raw_response_keeps_useful_or_non_assistant_messages(response: SimpleNamespace):
    item = RawResponse(response=response)

    assert not _is_empty_raw_response(item)


def test_before_query_drops_empty_raw_response_before_adding_continuation():
    empty_response = RawResponse(response=SimpleNamespace(role="assistant", content=None, tool_calls=None))
    text_response = RawResponse(response=SimpleNamespace(role="assistant", content="working", tool_calls=None))

    history = _before_query([TextInput(text="problem"), empty_response, text_response], last_error=None)

    assert empty_response not in history
    assert history[-1] == TextInput(
        text=(
            "Continue your solution attempt. You can use lean_loogle and lean_run_code "
            "tools as needed. When your solution is complete, call submit_proof - your "
            "work is only graded if you call this tool, and you cannot continue after "
            "calling it."
        )
    )


def test_before_query_reraises_last_error():
    error = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _before_query([], last_error=error)


def test_run_agent_loads_gateway_metadata_before_reading_capabilities(monkeypatch):
    events = []

    class FakeModel:
        metadata_loaded = False

        async def ensure_metadata_loaded(self):
            events.append("metadata")
            self.metadata_loaded = True

        @property
        def supports_tools(self):
            events.append("supports_tools")
            assert self.metadata_loaded
            return True

    class FakeAgent:
        def __init__(self, **kwargs):
            events.append("agent")

        async def run(self, **kwargs):
            events.append("run")
            return "result"

    model = FakeModel()
    monkeypatch.setattr(agent_module, "get_registry_model", lambda model_str: model)
    monkeypatch.setattr(agent_module, "Agent", FakeAgent)
    monkeypatch.setattr(agent_module, "SubmitProofTool", lambda *args: object())

    result = asyncio.run(agent_module.run_agent("openai/test", "prove it"))

    assert result == "result"
    assert events[:2] == ["metadata", "supports_tools"]
