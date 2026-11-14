import pytest

from video_workflow.llm.base import LLMError, extract_json, parse_into
from video_workflow.models import (
    Idea,
    IdeaBatch,
    Packaging,
    RenderPlan,
    Review,
    Script,
    Shot,
    ShotPrompt,
    SocialMeta,
    YouTubeMeta,
)

LLM_SCHEMAS = [Idea, IdeaBatch, Shot, Script, ShotPrompt, RenderPlan, Review, YouTubeMeta,
               SocialMeta, Packaging]


@pytest.mark.parametrize("schema", LLM_SCHEMAS, ids=lambda s: s.__name__)
def test_llm_schemas_are_strict_friendly(schema):
    """Every provider's structured-output mode needs all fields required and no defaults."""
    for name, field in schema.model_fields.items():
        assert field.is_required(), f"{schema.__name__}.{name} must be required (no default)"
    js = schema.model_json_schema()
    assert set(js["required"]) == set(js["properties"])


def test_extract_json_handles_fences_and_prose():
    text = 'Sure! Here you go:\n```json\n{"a": "x}y", "b": {"c": 1}}\n```\nanything else?'
    assert extract_json(text) == '{"a": "x}y", "b": {"c": 1}}'
    assert extract_json('noise {"k": "v \\" }"} trailing') == '{"k": "v \\" }"}'


def test_parse_into_validates():
    with pytest.raises(LLMError):
        parse_into(SocialMeta, '{"caption": "hi"}')
    assert parse_into(SocialMeta, '{"caption": "hi", "hashtags": []}').caption == "hi"
