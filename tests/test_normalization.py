"""Нормализация данных: эвристики, режимы, мета, защита от поломки схемы."""

from __future__ import annotations

from httpx import AsyncClient

from app.normalization import (
    apply_normalization,
    normalize_amount,
    normalize_currency,
    normalize_date,
    normalize_phone,
)
from tests.conftest import ADMIN_KEY

DATE_SCHEMA = {
    "type": "object",
    "properties": {
        "due_date": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]},
        "total_amount": {"type": ["number", "string", "null"]},
    },
    "additionalProperties": True,
}


# ------------------------------------------------------------------- юниты


def test_date_iso_kept() -> None:
    assert normalize_date("2026-05-15") == ("2026-05-15", "high")


def test_date_dot_ru() -> None:
    assert normalize_date("15.05.2026") == ("2026-05-15", "high")


def test_date_two_digit_year_medium() -> None:
    normalized, confidence = normalize_date("15.05.26")
    assert normalized == "2026-05-15"
    assert confidence == "medium"


def test_date_textual_ru() -> None:
    assert normalize_date("15 мая 2026") == ("2026-05-15", "high")


def test_date_textual_en() -> None:
    assert normalize_date("3 December 2025") == ("2025-12-03", "high")


def test_date_garbage_low() -> None:
    assert normalize_date("как только") == (None, "low")
    assert normalize_date("32.13.2026") == (None, "low")


def test_currency_variants() -> None:
    assert normalize_currency("RUB") == ("RUB", "high")
    assert normalize_currency("₽") == ("RUB", "high")
    assert normalize_currency("руб.") == ("RUB", "medium")
    assert normalize_currency("евро") == ("EUR", "medium")
    assert normalize_currency("не валюта") == (None, "low")


def test_amount_variants() -> None:
    assert normalize_amount("1500") == (1500, "high")
    assert normalize_amount("1 500,50") == (1500.5, "high")
    assert normalize_amount("1,500.50") == (1500.5, "high")
    assert normalize_amount("1 500") == (1500, "high")
    assert normalize_amount("1.500") == (1500, "medium")
    assert normalize_amount("abc") == (None, "low")


def test_phone_variants() -> None:
    assert normalize_phone("+7 916 555-77-88") == ("+79165557788", "high")
    assert normalize_phone("89165557788") == ("+79165557788", "high")
    assert normalize_phone("9165557788") == ("+79165557788", "medium")
    assert normalize_phone("звонить утром") == (None, "low")


def test_off_mode_changes_nothing() -> None:
    data = {"due_date": "15.05.2026"}
    normalized, meta = apply_normalization(data, DATE_SCHEMA, "off")
    assert normalized == data
    assert meta["fields"] == []


def test_basic_mode_normalizes_dates_and_amounts() -> None:
    data = {"due_date": "15.05.26", "total_amount": "1 500,50", "currency": "руб."}
    normalized, meta = apply_normalization(data, DATE_SCHEMA, "basic")
    assert normalized["due_date"] == "2026-05-15"  # 15.05.26 → 2026-05-15
    assert normalized["total_amount"] == 1500.5
    assert normalized["currency"] == "RUB"
    assert meta["mode"] == "basic"
    assert {note["path"] for note in meta["fields"]} == {"$.due_date", "$.total_amount", "$.currency"}


def test_strict_mode_skips_medium_confidence() -> None:
    data = {"due_date": "15.05.26", "total_amount": "1.500"}
    normalized, meta = apply_normalization(data, DATE_SCHEMA, "strict")
    # 2-значный год и неоднозначные разделители — не трогаем
    assert normalized["due_date"] == "15.05.26"
    assert normalized["total_amount"] == "1.500"
    assert meta["fields"] == []


def test_low_confidence_keeps_raw_value() -> None:
    data = {"due_date": "после праздников"}
    normalized, meta = apply_normalization(data, DATE_SCHEMA, "basic")
    assert normalized["due_date"] == "после праздников"


def test_normalization_note_contains_raw_and_normalized() -> None:
    data = {"due_date": "15 мая 2026"}
    _, meta = apply_normalization(data, DATE_SCHEMA, "strict")
    note = meta["fields"][0]
    assert note["raw_value"] == "15 мая 2026"
    assert note["normalized_value"] == "2026-05-15"
    assert note["confidence"] == "high"
    assert note["path"] == "$.due_date"


def test_schema_hint_format_date() -> None:
    schema = {"type": "object", "properties": {"when": {"type": ["string", "null"], "format": "date"}}}
    data = {"when": "15.05.2026"}
    normalized, _ = apply_normalization(data, schema, "strict")
    assert normalized["when"] == "2026-05-15"


def test_x_normalize_hint() -> None:
    schema = {"type": "object", "properties": {"money": {"x-normalize": "amount"}}}
    data = {"money": "2 400"}
    normalized, _ = apply_normalization(data, schema, "strict")
    assert normalized["money"] == 2400


def test_nested_structures_walked() -> None:
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}}
    data = {"items": [{"price": "1 200"}, {"price": "300"}]}
    normalized, _ = apply_normalization(data, schema, "strict")
    assert normalized["items"][0]["price"] == 1200
    assert normalized["items"][1]["price"] == 300


# -------------------------------------------------------------------- API


def _auth(key: dict) -> dict[str, str]:
    return {"X-API-Key": key["api_key"]}


async def test_transform_meta_normalization(client: AsyncClient, fake_llm) -> None:
    key = (
        await client.post(
            "/admin/api-keys", json={"name": "n", "credits": 50}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()
    fake_llm.script('{"due_date": "15.05.2026", "currency": "руб.", "total_amount": "1 500,50"}')
    resp = await client.post(
        "/v1/transform",
        json={
            "raw_text": "счёт",
            "schema": DATE_SCHEMA,
            "options": {"normalization": "basic"},
        },
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["due_date"] == "2026-05-15"
    assert body["data"]["currency"] == "RUB"
    assert body["data"]["total_amount"] == 1500.5
    normalization = body["meta"]["normalization"]
    assert normalization["mode"] == "basic"
    assert len(normalization["fields"]) == 3


async def test_normalization_reverted_if_breaks_schema(client: AsyncClient, fake_llm) -> None:
    """Нормализация не должна ломать схему: иначе изменения откатываются."""
    strict_schema = {
        "type": "object",
        "properties": {"total_amount": {"type": "string"}},
        "required": ["total_amount"],
    }
    key = (
        await client.post(
            "/admin/api-keys", json={"name": "r", "credits": 50}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()
    fake_llm.script('{"total_amount": "1 500"}')
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "счёт", "schema": strict_schema, "options": {"normalization": "basic"}},
        headers=_auth(key),
    )
    assert resp.status_code == 200, resp.text
    # schema требует string — число сломало бы валидацию, поэтому откат
    assert resp.json()["data"]["total_amount"] == "1 500"


async def test_invalid_normalization_mode_rejected(client: AsyncClient) -> None:
    key = (
        await client.post(
            "/admin/api-keys", json={"name": "bad", "credits": 50}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()
    resp = await client.post(
        "/v1/transform",
        json={"raw_text": "x", "options": {"normalization": "ultra"}},
        headers=_auth(key),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


async def test_provenance_meta(client: AsyncClient, fake_llm) -> None:
    key = (
        await client.post(
            "/admin/api-keys", json={"name": "p", "credits": 50}, headers={"X-Admin-Key": ADMIN_KEY}
        )
    ).json()
    fake_llm.script('{"name": "Иван Петров", "city": "Москва"}')
    resp = await client.post(
        "/v1/transform",
        json={
            "raw_text": "Клиент Иван Петров из Москва, просит счёт",
            "options": {"include_provenance": True},
        },
        headers=_auth(key),
    )
    assert resp.status_code == 200
    provenance = resp.json()["meta"]["provenance"]
    paths = {field["path"] for field in provenance["fields"]}
    assert "$.name" in paths and "$.city" in paths
    for field in provenance["fields"]:
        assert field["excerpt"], "каждое поле должно цитировать источник"
        assert len(field["excerpt"]) <= 120
