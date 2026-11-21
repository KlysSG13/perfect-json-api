"""Утилиты для работы с JSON и JSON Schema.

- Извлечение первого валидного JSON из ответа LLM (включая очистку markdown).
- Нормализация и безопасная проверка пользовательских схем.
- Валидация данных со человекочитаемыми ошибками.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from app.exceptions import InvalidSchemaError

DRAFT_2020_12_URI = "https://json-schema.org/draft/2020-12/schema"

MAX_SCHEMA_CHARS = 64_000
MAX_SCHEMA_DEPTH = 64
MAX_PATTERN_LENGTH = 512
MAX_VALIDATION_ERRORS = 10

_REMOTE_REF_PREFIXES = ("http://", "https://", "urn:", "//")


def strip_bom(text: str) -> str:
    if text.startswith("\ufeff"):
        return text[1:]
    return text


def _try_loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _strip_markdown_fences(text: str) -> str:
    """Убирает обёртки вида ```json ... ``` или ``` ... ```."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2:
        first = lines[0].strip()
        last = lines[-1].strip()
        if first.startswith("```") and last.startswith("```"):
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _find_balanced(text: str, start: int) -> int | None:
    """Ищет конец сбалансированного объекта/массива, начатого на позиции start."""
    open_char = text[start]
    close_char = "}" if open_char == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0 and ch == close_char:
                return i + 1
    return None


def extract_json(text: str) -> Any | None:
    """Извлекает первый валидный JSON-объект или массив из текста.

    Порядок попыток:
    1. Прямой парсинг (после удаления BOM и trim).
    2. Парсинг после снятия markdown-обёрток.
    3. Скан сбалансированных {...} / [...] внутри текста.
    """
    if not text:
        return None
    cleaned = strip_bom(text).strip()
    if not cleaned:
        return None

    direct = _try_loads(cleaned)
    if direct is not None:
        return direct

    without_fences = _strip_markdown_fences(cleaned)
    if without_fences != cleaned:
        parsed = _try_loads(without_fences)
        if parsed is not None:
            return parsed

    for i, ch in enumerate(without_fences):
        if ch not in "{[":
            continue
        end = _find_balanced(without_fences, i)
        if end is None:
            continue
        candidate = without_fences[i:end]
        parsed = _try_loads(candidate)
        if parsed is not None:
            return parsed
    return None


def normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Добавляет $schema Draft 2020-12, если он не указан."""
    normalized = dict(schema)
    if "$schema" not in normalized:
        normalized["$schema"] = DRAFT_2020_12_URI
    return normalized


def _walk_schema_for_safety(node: Any, path: str, depth: int, problems: list[str]) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        problems.append(f"{path or '$'}: schema nesting depth exceeds {MAX_SCHEMA_DEPTH}")
        return
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and any(ref.startswith(p) for p in _REMOTE_REF_PREFIXES):
            problems.append(
                f"{path or '$'}.$ref: remote references are not allowed ({ref[:60]}...)"
            )
        for key in ("pattern", "patternProperties"):
            value = node.get(key)
            if key == "pattern" and isinstance(value, str) and len(value) > MAX_PATTERN_LENGTH:
                problems.append(
                    f"{path or '$'}.{key}: pattern longer than {MAX_PATTERN_LENGTH} chars is not allowed"
                )
            if key == "patternProperties" and isinstance(value, dict):
                for pattern in value:
                    if len(pattern) > MAX_PATTERN_LENGTH:
                        problems.append(
                            f"{path or '$'}.{key}: pattern longer than {MAX_PATTERN_LENGTH} chars "
                            "is not allowed"
                        )
        for key, value in node.items():
            if key in ("properties", "items", "anyOf", "oneOf", "allOf", "not", "additionalProperties"):
                _walk_schema_for_safety(value, f"{path}.{key}" if path else f"$.{key}", depth + 1, problems)
            elif isinstance(value, dict):
                _walk_schema_for_safety(value, f"{path}.{key}" if path else f"$.{key}", depth + 1, problems)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _walk_schema_for_safety(item, f"{path}[{idx}]", depth + 1, problems)


def validate_user_schema(schema: Any) -> dict[str, Any]:
    """Проверяет пользовательскую схему и возвращает нормализованную версию.

    Бросает InvalidSchemaError (422, invalid_schema) с человекочитаемыми
    причинами. Проверки: синтаксис Draft 2020-12, размер, глубина,
    запрет удалённых $ref и сверхдлинных regex-паттернов (ReDoS).
    """
    if not isinstance(schema, dict):
        raise InvalidSchemaError("Schema must be a JSON object", details={"errors": [
            "schema: expected a JSON object"
        ]})

    serialized = json.dumps(schema, ensure_ascii=False)
    if len(serialized) > MAX_SCHEMA_CHARS:
        raise InvalidSchemaError(
            f"Schema is too large ({len(serialized)} chars, max {MAX_SCHEMA_CHARS})",
            details={"errors": [f"schema: serialized size exceeds {MAX_SCHEMA_CHARS} chars"]},
        )

    problems: list[str] = []
    _walk_schema_for_safety(schema, "", 0, problems)
    if problems:
        raise InvalidSchemaError("Schema contains unsafe constructs", details={"errors": problems})

    normalized = normalize_schema(schema)
    try:
        Draft202012Validator.check_schema(normalized)
    except SchemaError as exc:
        raise InvalidSchemaError(
            "Schema is not a valid JSON Schema (Draft 2020-12)",
            details={"errors": [_human_schema_error(exc)]},
        ) from exc
    return normalized


def _human_schema_error(exc: SchemaError) -> str:
    path = "$" + "".join(f".{p}" if not isinstance(p, int) else f"[{p}]" for p in exc.absolute_path)
    return f"{path}: {exc.message}"


def validate_data(data: Any, schema: dict[str, Any], max_errors: int = MAX_VALIDATION_ERRORS) -> list[str]:
    """Валидирует данные по схеме; возвращает не более max_errors описаний."""
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for err in sorted(validator.iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path]):
        errors.append(_format_validation_error(err))
        if len(errors) >= max_errors:
            break
    return errors


def _format_validation_error(err: ValidationError) -> str:
    path = "$" + "".join(
        f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path
    )
    return f"{path}: {err.message}"
