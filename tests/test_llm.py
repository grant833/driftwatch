from types import SimpleNamespace

import pytest

from driftwatch.llm import AnthropicLLM


class FakeMessages:
    def __init__(self, resp):
        self.resp, self.kwargs = resp, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.resp


def make(text, stop="end_turn"):
    llm = AnthropicLLM.__new__(AnthropicLLM)
    from anthropic import transform_schema
    llm._transform = transform_schema
    resp = SimpleNamespace(
        stop_reason=stop,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    llm.client = SimpleNamespace(messages=FakeMessages(resp))
    return llm


SCHEMA = {"type": "object", "properties": {"p": {"type": "number", "minimum": 0}},
          "required": ["p"]}


def test_sends_output_config_and_parses_json():
    llm = make('{"p": 0.7}')
    res = llm.call_json("m", "sys", "user", SCHEMA)
    assert res.data == {"p": 0.7} and res.input_tokens == 10
    sent = llm.client.messages.kwargs
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert "tools" not in sent and "tool_choice" not in sent


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_unusable_stop_reasons_raise(stop):
    with pytest.raises(ValueError):
        make('{"p": 0.7}', stop=stop).call_json("m", "s", "u", SCHEMA)
