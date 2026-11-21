"""Provenance: эвристические ссылки на источник для ключевых значений.

Без второго LLM-вызова: для каждого скалярного поля результата ищется
фрагмент входного текста, из которого значение могло быть извлечено.
Возвращаются только координаты и короткая цитата (не весь вход).
"""

from __future__ import annotations

from typing import Any

MAX_FIELDS = 20
EXCERPT_CHARS = 80
MIN_VALUE_LEN = 3


def find_provenance(data: Any, source_text: str, *, max_fields: int = MAX_FIELDS) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    lowered_source = source_text.lower()
    for path, value in _iter_leaves(data, "$", 0):
        if len(fields) >= max_fields:
            break
        needle = _needle(value)
        if needle is None:
            continue
        position = lowered_source.find(needle.lower())
        if position < 0:
            continue
        start = max(0, position)
        end = min(len(source_text), position + len(needle))
        fields.append(
            {
                "path": path,
                "start": start,
                "end": end,
                "excerpt": source_text[start : start + EXCERPT_CHARS].strip(),
            }
        )
    return {"fields": fields}


def _iter_leaves(node: Any, path: str, depth: int):
    if depth > 4:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_leaves(value, f"{path}.{key}", depth + 1)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_leaves(value, f"{path}[{index}]", depth + 1)
    else:
        yield path, node


def _needle(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = str(value)
        return text if len(text) >= MIN_VALUE_LEN else None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if len(stripped) >= MIN_VALUE_LEN else None
    return None
