from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from facelink.models import SceneSnapshot, ShotSpec
from facelink.provider import plan_with_openai


def test_openai_provider_uses_typed_response_and_configuration():
    snapshot = SceneSnapshot(scene_name="Scene")
    parsed = ShotSpec(
        title="Empty establishing shot",
        fps=24,
        duration=2,
        beats=[],
    )
    responses = Mock()
    responses.parse.return_value = SimpleNamespace(output_parsed=parsed)
    client = SimpleNamespace(responses=responses)
    with patch("facelink.provider.OpenAI", return_value=client) as constructor:
        result = plan_with_openai(
            "Hold for two seconds",
            snapshot,
            api_key="test-key",
            model="test-model",
            base_url="https://example.invalid/v1",
        )
    assert result.title == "Empty establishing shot"
    constructor.assert_called_once_with(api_key="test-key", base_url="https://example.invalid/v1")
    kwargs = responses.parse.call_args.kwargs
    assert kwargs["text_format"].__name__ == "ShotSpec"
    assert kwargs["model"] == "test-model"
    assert "Scene snapshot" in kwargs["input"][1]["content"]


def test_openai_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        plan_with_openai("brief", SceneSnapshot(scene_name="Scene"))


def test_openai_provider_rejects_empty_parsed_output():
    responses = Mock()
    responses.parse.return_value = SimpleNamespace(output_parsed=None)
    client = SimpleNamespace(responses=responses)
    with (
        patch("facelink.provider.OpenAI", return_value=client),
        pytest.raises(RuntimeError, match="valid ShotSpec"),
    ):
        plan_with_openai("brief", SceneSnapshot(scene_name="Scene"), api_key="test-key")
