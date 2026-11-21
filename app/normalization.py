"""Осторожная нормализация извлечённых данных.

Режимы:
- off — ничего не меняет;
- basic — уверенные (high) и умеренные (medium) преобразования;
- strict — только однозначные (high) преобразования.

Правила осторожности:
- низкая уверенность → исходное значение сохраняется + предупреждение;
- после нормализации вызывающий код перепроверяет данные по схеме и
  при неуспехе откатывает изменения (см. TransformService).

Подсказки берутся из имени поля и из схемы (format: date,
x-normalize: date|currency|amount|phone).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

Mode = str  # "off" | "basic" | "strict"

MAX_DEPTH = 8
MAX_NOTES = 50

RU_MONTHS = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTHS = {**RU_MONTHS, **EN_MONTHS}

CURRENCY_MAP = {
    "rub": "RUB", "rur": "RUB", "₽": "RUB", "руб": "RUB", "рублей": "RUB", "рубля": "RUB", "р.": "RUB",
    "usd": "USD", "$": "USD", "доллар": "USD", "доллара": "USD", "долларов": "USD",
    "eur": "EUR", "€": "EUR", "евро": "EUR",
    "gbp": "GBP", "£": "GBP",
    "kzt": "KZT", "₸": "KZT", "тенге": "KZT",
    "uah": "UAH", "₴": "UAH", "грн": "UAH", "гривна": "UAH",
}

DATE_NAME_HINTS = ("date", "due", "deadline", "when", "срок", "дат")
CURRENCY_NAME_HINTS = ("currency", "валют")
AMOUNT_NAME_HINTS = (
    "amount", "total", "price", "budget", "sum", "cost",
    "сумм", "стоимост", "цена", "бюджет", "итог",
)
PHONE_NAME_HINTS = ("phone", "телефон", "tel")

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_NUMERIC_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{2,4})$")
_TEXTUAL_DATE_RE = re.compile(
    r"^(\d{1,2})\s+([a-zA-Zа-яА-ЯёЁ]+)\s+(\d{4})$"
)
_AMOUNT_RE = re.compile(r"^-?\d[\d\s\u00a0.,]*$")


def _key_hints_date(path_last: str, schema_hint: str | None) -> bool:
    lowered = path_last.lower()
    return schema_hint == "date" or any(h in lowered for h in DATE_NAME_HINTS)


def _key_hints_currency(path_last: str, schema_hint: str | None) -> bool:
    lowered = path_last.lower()
    return schema_hint == "currency" or any(h in lowered for h in CURRENCY_NAME_HINTS)


def _key_hints_amount(path_last: str, schema_hint: str | None) -> bool:
    lowered = path_last.lower()
    return schema_hint == "amount" or any(h in lowered for h in AMOUNT_NAME_HINTS)


def _key_hints_phone(path_last: str, schema_hint: str | None) -> bool:
    lowered = path_last.lower()
    return schema_hint == "phone" or any(h in lowered for h in PHONE_NAME_HINTS)


def normalize_date(value: str) -> tuple[str | None, str]:
    """Возвращает (нормализованная ISO-дата | None, confidence)."""
    value = value.strip().rstrip(".")
    iso = _ISO_DATE_RE.match(value)
    if iso:
        try:
            date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
            return value, "high"
        except ValueError:
            return None, "low"
    numeric = _NUMERIC_RE.match(value)
    if numeric:
        day, month, year = (int(numeric.group(i)) for i in (1, 2, 3))
        if year < 100:
            year += 2000 if year < 70 else 1900
            confidence = "medium"
        else:
            confidence = "high"
        try:
            return date(year, month, day).isoformat(), confidence
        except ValueError:
            return None, "low"
    textual = _TEXTUAL_DATE_RE.match(value)
    if textual:
        month = MONTHS.get(textual.group(2).lower())
        if month:
            try:
                return date(int(textual.group(3)), month, int(textual.group(1))).isoformat(), "high"
            except ValueError:
                return None, "low"
    return None, "low"


def normalize_currency(value: str) -> tuple[str | None, str]:
    """Возвращает (ISO 4217 | None, confidence)."""
    token = value.strip().lower().rstrip(".")
    if not token:
        return None, "low"
    mapped = CURRENCY_MAP.get(token)
    if mapped is None:
        return None, "low"
    confidence = "high" if token in {"rub", "usd", "eur", "gbp", "kzt", "uah", "₽", "$", "€", "£", "₸", "₴"} else "medium"
    return mapped, confidence


def normalize_amount(value: str) -> tuple[int | float | None, str]:
    """Возвращает (число | None, confidence)."""
    token = value.strip().replace("\u00a0", " ")
    if not _AMOUNT_RE.match(token):
        return None, "low"
    cleaned = token.replace(" ", "")
    has_comma = "," in cleaned
    has_dot = "." in cleaned
    if has_comma and has_dot:
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        cleaned = cleaned.replace(thousands_sep, "")
        cleaned = cleaned.replace(decimal_sep, ".")
        confidence = "high"
    elif has_comma:
        if re.fullmatch(r"-?\d{1,3}(,\d{3})+", cleaned) and not re.search(r",\d{1,2}$", cleaned):
            cleaned = cleaned.replace(",", "")
            confidence = "high"
        elif re.fullmatch(r"-?\d+,\d{1,2}$", cleaned):
            cleaned = cleaned.replace(",", ".")
            confidence = "high"
        else:
            cleaned = cleaned.replace(",", ".")
            confidence = "medium"
    elif has_dot:
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", cleaned) and not re.search(r"\.\d{1,2}$", cleaned):
            cleaned = cleaned.replace(".", "")
            confidence = "medium"
        else:
            confidence = "high"
    else:
        confidence = "high"
    try:
        number = float(cleaned)
    except ValueError:
        return None, "low"
    if number.is_integer():
        number = int(number)
    return number, confidence


def normalize_phone(value: str) -> tuple[str | None, str]:
    """Возвращает (E.164 | None, confidence). Осторожно: только с подсказкой."""
    raw = value.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 11 <= len(digits) <= 15:
        return f"+{digits}", "high"
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return "+7" + digits[1:], "high"
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits, "medium"
    return None, "low"


class Normalizer:
    """Обход результата и осторожная нормализация значений."""

    def __init__(self, mode: Mode, schema: dict[str, Any] | None = None) -> None:
        self.mode = mode
        self.schema = schema or {}
        self.notes: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ api

    def normalize(self, data: Any) -> Any:
        if self.mode == "off":
            return data
        return self._walk(data, "$", self.schema, 0)

    # -------------------------------------------------------------- internal

    def _walk(self, node: Any, path: str, schema: dict[str, Any], depth: int) -> Any:
        if depth > MAX_DEPTH or len(self.notes) >= MAX_NOTES:
            return node
        if isinstance(node, dict):
            properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
            result = {}
            for key, value in node.items():
                child_schema = properties.get(key) if isinstance(properties, dict) else None
                if not isinstance(child_schema, dict):
                    child_schema = {}
                if isinstance(value, (dict, list)):
                    result[key] = self._walk(value, f"{path}.{key}", child_schema, depth + 1)
                elif isinstance(value, str):
                    child_path = f"{path}.{key}"
                    result[key] = self._normalize_scalar(value, key, child_path, child_schema)
                else:
                    result[key] = value
            return result
        if isinstance(node, list):
            items_schema = schema.get("items", {}) if isinstance(schema, dict) else {}
            if not isinstance(items_schema, dict):
                items_schema = {}
            return [
                self._walk(item, f"{path}[{index}]", items_schema, depth + 1)
                for index, item in enumerate(node)
            ]
        return node

    def _normalize_scalar(
        self, value: str, key: str, path: str, field_schema: dict[str, Any]
    ) -> Any:
        schema_hint = field_schema.get("x-normalize")
        fmt = field_schema.get("format")
        if fmt in ("date", "date-time") and schema_hint is None:
            schema_hint = "date"
        stripped = value.strip()
        if not stripped:
            return value

        if _key_hints_date(key, schema_hint):
            normalized, confidence = normalize_date(stripped)
            if normalized is not None and normalized != stripped:
                if self._allowed(confidence):
                    self._note(path, value, normalized, confidence)
                    return normalized
                if confidence == "low":
                    self._note(path, value, stripped, "low", warning="date format not recognized; kept as is")
            return value

        if _key_hints_currency(key, schema_hint):
            normalized, confidence = normalize_currency(stripped)
            if normalized is not None and normalized != stripped:
                if self._allowed(confidence):
                    self._note(path, value, normalized, confidence)
                    return normalized
            return value

        if _key_hints_phone(key, schema_hint):
            normalized, confidence = normalize_phone(stripped)
            if normalized is not None:
                if self._allowed(confidence):
                    self._note(path, value, normalized, confidence)
                    return normalized
                self._note(path, value, stripped, "low", warning="phone format ambiguous; kept as is")
            return value

        if _key_hints_amount(key, schema_hint):
            normalized, confidence = normalize_amount(stripped)
            if normalized is not None and normalized != value:
                if self._allowed(confidence):
                    self._note(path, value, normalized, confidence)
                    return normalized
            return value

        return value

    def _allowed(self, confidence: str) -> bool:
        if self.mode == "strict":
            return confidence == "high"
        if self.mode == "basic":
            return confidence in ("high", "medium")
        return False

    def _note(
        self,
        path: str,
        raw_value: Any,
        normalized_value: Any,
        confidence: str,
        warning: str | None = None,
    ) -> None:
        note: dict[str, Any] = {
            "path": path,
            "raw_value": _short(raw_value),
            "normalized_value": _short(normalized_value),
            "confidence": confidence,
        }
        if warning:
            note["warning"] = warning
        self.notes.append(note)


def _short(value: Any, limit: int = 200) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def apply_normalization(
    data: Any, schema: dict[str, Any], mode: Mode
) -> tuple[Any, dict[str, Any]]:
    """Нормализует данные и возвращает мета-блок для ответа."""
    normalizer = Normalizer(mode, schema)
    normalized = normalizer.normalize(data)
    meta = {"mode": mode, "fields": normalizer.notes}
    return normalized, meta
