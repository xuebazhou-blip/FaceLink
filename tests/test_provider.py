from types import SimpleNamespace
from unittest.mock import Mock, patch

from facelink.models import SceneSnapshot
from facelink.provider import plan_with_openai


def test_openai_provider_uses_typed_response():
    snapshot = SceneSnapshot(scene_name="Scene")
    parsed = {
        "schema_version": "1.0",
        "title": "Empty establishing shot",
        "fps": 24,
        "duration": 2,
        "beats": [],
    }
    responses = Mock()
    responses.parse.return_value = SimpleNamespace(output_parsed=parsed)
    client = SimpleNamespace(responses=responses)
    with patch("facelink.provider.OpenAI", return_value=client):
        result = plan_with_openai("Hold for two seconds", snapshot, api_key="test-key")
    assert result.title == "Empty establishing shot"
    assert responses.parse.call_args.kwargs["text_format"].__name__ == "ShotSpec"

