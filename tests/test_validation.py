"""Юнит-тесты json_utils и пресетов: извлечение JSON, схемы, безопасность."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from app.exceptions import InvalidSchemaError
from app.json_utils import extract_json, normalize_schema, validate_data, validate_user_schema
from app.presets import PRESETS

# ------------------------------------------------------------ extract_json


def test_extract_json_direct() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_bom_and_spaces() -> None:
    assert extract_json('﻿  {"a": 1}  \n') == {"a": 1}


def test_extract_json_markdown_fence() -> None:
    text = '```json\n{"a": {"b": 2}}\n```'
    assert extract_json(text) == {"a": {"b": 2}}


def test_extract_json_plain_fence() -> None:
    assert extract_json('```\n[1, 2, 3]\n```') == [1, 2, 3]


def test_extract_json_surrounded_by_text() -> None:
    text = 'Вот результат:\n{"name": "Иван", "tags": ["a", "b"]}\nСпасибо!'
    assert extract_json(text) == {"name": "Иван", "tags": ["a", "b"]}


def test_extract_json_first_of_two() -> None:
    text = '{"first": true} и потом {"second": true}'
    assert extract_json(text) == {"first": True}


def test_extract_json_array_top_level() -> None:
    assert extract_json("результат: [1, 2, {\"x\": 1}] конец") == [1, 2, {"x": 1}]


def test_extract_json_braces_inside_strings() -> None:
    text = '{"code": "if (x) { return 1; }"}'
    assert extract_json(text) == {"code": "if (x) { return 1; }"}


def test_extract_json_invalid_returns_none() -> None:
    assert extract_json("никакого json здесь нет") is None
    assert extract_json("") is None
    assert extract_json("{unclosed: 1}") is None


def test_extract_json_null_and_numbers() -> None:
    assert extract_json('{"n": null, "f": 1.5, "e": 1e3}') == {"n": None, "f": 1.5, "e": 1000.0}


# ---------------------------------------------------------------- presets


def test_all_presets_are_valid_schemas() -> None:
    for name, schema in PRESETS.items():
        Draft202012Validator.check_schema(schema)  # бросит исключение, если схема невалидна
        assert schema["$schema"].startswith("https://json-schema.org/draft/2020-12"), name


def test_universal_accepts_any_object() -> None:
    schema = PRESETS["universal_v1"]
    assert validate_data({"anything": {"nested": [1, 2, 3]}}, schema) == []


def test_invoice_requires_core_fields() -> None:
    schema = PRESETS["invoice_v1"]
    assert validate_data({"currency": "RUB", "total_amount": 100, "line_items": []}, schema) == []
    errors = validate_data({"currency": "RUB"}, schema)
    assert errors, "total_amount и line_items обязательны"


def test_lead_priority_enum() -> None:
    schema = PRESETS["lead_v1"]
    assert validate_data({"name": "A", "priority": "high"}, schema) == []
    errors = validate_data({"name": "A", "priority": "urgent"}, schema)
    assert errors


# ---------------------------------------------------------- schema checks


def test_validate_user_schema_rejects_non_object() -> None:
    with pytest.raises(InvalidSchemaError):
        validate_user_schema(["not", "a", "schema"])


def test_validate_user_schema_rejects_bad_type() -> None:
    with pytest.raises(InvalidSchemaError) as exc_info:
        validate_user_schema({"type": 123})
    assert exc_info.value.details["errors"]


def test_validate_user_schema_rejects_remote_ref() -> None:
    schema = {"type": "object", "$ref": "https://evil.example.com/schema.json"}
    with pytest.raises(InvalidSchemaError) as exc_info:
        validate_user_schema(schema)
    assert any("remote" in error for error in exc_info.value.details["errors"])


def test_validate_user_schema_rejects_long_pattern() -> None:
    schema = {"type": "object", "properties": {"x": {"type": "string", "pattern": "a*" * 400}}}
    with pytest.raises(InvalidSchemaError):
        validate_user_schema(schema)


def test_validate_user_schema_rejects_huge_schema() -> None:
    schema = {"type": "object", "properties": {f"field_{i}": {"type": "string"} for i in range(6000)}}
    with pytest.raises(InvalidSchemaError):
        validate_user_schema(schema)


def test_normalize_schema_adds_draft() -> None:
    normalized = normalize_schema({"type": "object"})
    assert normalized["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_validate_user_schema_ok_and_normalized() -> None:
    normalized = validate_user_schema({"type": "object", "properties": {"n": {"type": "number"}}})
    assert normalized["$schema"].startswith("https://json-schema.org/draft/2020-12")
    Draft202012Validator.check_schema(normalized)


def test_validate_data_human_readable_errors() -> None:
    schema = {"type": "object", "properties": {"age": {"type": "integer"}}, "required": ["age"]}
    errors = validate_data({"age": "old"}, schema)
    assert errors and errors[0].startswith("$.age:")


def test_validate_data_max_ten_errors() -> None:
    schema = {"type": "array", "items": {"type": "integer"}}
    errors = validate_data(["no"] * 50, schema)
    assert len(errors) == 10


def test_schema_json_roundtrip() -> None:
    schema = validate_user_schema({"type": "object"})
    assert json.loads(json.dumps(schema)) == schema
