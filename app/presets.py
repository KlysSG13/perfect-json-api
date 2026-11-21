"""Встроенные пресеты JSON Schema (Draft 2020-12).

Все пресеты — валидные схемы, хранятся в коде проекта.
Пресеты намеренно умеренно строгие: обязательные поля — только те,
без которых структура теряет смысл; большинство полей допускают null.
"""

from __future__ import annotations

from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    "universal_v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Universal JSON",
        "description": "Любой JSON-объект; дополнительные свойства разрешены.",
        "type": "object",
        "additionalProperties": True,
    },
    "invoice_v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Invoice",
        "description": "Счёт или коммерческое предложение.",
        "type": "object",
        "properties": {
            "currency": {
                "anyOf": [
                    {"type": "string", "minLength": 1},
                    {"type": "null"},
                ],
                "description": "Валюта, например RUB, USD, EUR.",
            },
            "total_amount": {
                "anyOf": [
                    {"type": "number"},
                    {"type": "string", "minLength": 1},
                ],
                "description": "Итоговая сумма.",
            },
            "due_date": {
                "anyOf": [
                    {"type": "string", "minLength": 1},
                    {"type": "null"},
                ],
                "description": "Срок оплаты.",
            },
            "issuer": {
                "anyOf": [
                    {"type": "object"},
                    {"type": "string", "minLength": 1},
                    {"type": "null"},
                ],
                "description": "Поставщик/исполнитель.",
            },
            "customer": {
                "anyOf": [
                    {"type": "object"},
                    {"type": "string", "minLength": 1},
                    {"type": "null"},
                ],
                "description": "Плательщик/заказчик.",
            },
            "line_items": {
                "type": "array",
                "description": "Позиции счёта.",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "quantity": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                        "unit_price": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                        "total": {"anyOf": [{"type": "number"}, {"type": "string"}, {"type": "null"}]},
                    },
                },
            },
            "notes": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "payment_instructions": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["currency", "total_amount", "line_items"],
        "additionalProperties": True,
    },
    "lead_v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Lead",
        "description": "Лид из любого хаотичного описания.",
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "company": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "contact": {
                "anyOf": [
                    {"type": "object"},
                    {"type": "string"},
                    {"type": "null"},
                ]
            },
            "budget": {
                "anyOf": [
                    {"type": "number"},
                    {"type": "string"},
                    {"type": "null"},
                ]
            },
            "need": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "timeline": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "source": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "priority": {
                "anyOf": [
                    {"type": "string", "enum": ["low", "medium", "high"]},
                    {"type": "null"},
                ]
            },
        },
        "required": ["name"],
        "additionalProperties": True,
    },
    "product_card_v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Product card",
        "description": "Товарная карточка.",
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "price": {
                "anyOf": [
                    {"type": "number"},
                    {"type": "string"},
                    {"type": "null"},
                ]
            },
            "currency": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "sku": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "attributes": {
                "anyOf": [
                    {"type": "object"},
                    {"type": "null"},
                ],
                "description": "Произвольные атрибуты товара.",
            },
            "tags": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "null"},
                ]
            },
        },
        "required": ["name"],
        "additionalProperties": True,
    },
    "resume_v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Resume",
        "description": "Резюме кандидата.",
        "type": "object",
        "properties": {
            "full_name": {"type": "string", "minLength": 1},
            "role": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "skills": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "null"},
                ]
            },
            "experience": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "company": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "position": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "start_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "end_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            },
            "education": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "institution": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "degree": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "year": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "number"},
                                {"type": "null"},
                            ]
                        },
                    },
                },
            },
            "contact": {
                "anyOf": [
                    {"type": "object"},
                    {"type": "null"},
                ]
            },
        },
        "required": ["full_name"],
        "additionalProperties": True,
    },
}

DEFAULT_PRESET = "universal_v1"


def get_preset(name: str) -> dict[str, Any] | None:
    return PRESETS.get(name)


def list_preset_names() -> list[str]:
    return list(PRESETS.keys())
