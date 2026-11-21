"""Eval-фреймворк: датасеты валидны, раннер работает офлайн, метрики считаются."""

from __future__ import annotations

import json
from pathlib import Path

from app.eval.run import (
    ScriptedGoldLLM,
    load_datasets,
    run_eval,
    validate_gold,
)
from tests.conftest import make_test_settings

DATASETS_DIR = Path(__file__).parent.parent / "app" / "eval" / "datasets"


def _datasets() -> list[dict]:
    return load_datasets(DATASETS_DIR)


def test_datasets_loaded() -> None:
    cases = _datasets()
    assert len(cases) >= 50


def test_each_preset_has_five_cases() -> None:
    cases = _datasets()
    by_preset: dict[str, int] = {}
    for case in cases:
        by_preset[case["preset"]] = by_preset.get(case["preset"], 0) + 1
    for preset in ("universal_v1", "invoice_v1", "lead_v1", "product_card_v1", "resume_v1"):
        assert by_preset.get(preset, 0) >= 5, preset


def test_case_structure() -> None:
    for case in _datasets():
        assert case.get("name"), case
        assert case.get("raw_text"), case["name"]
        assert "expected" in case, case["name"]
        assert "expected_fields" in case, case["name"]


def test_gold_passes_schemas() -> None:
    assert validate_gold(_datasets()) == []


def test_no_personal_data_in_datasets() -> None:
    """Датасеты синтетические: без реальных адресов/телефонов конечных людей."""
    text = json.dumps(_datasets(), ensure_ascii=False)
    for forbidden in ("паспорт", "снилс", "770123456789", "4111 1111 1111"):
        assert forbidden not in text.lower()


async def test_run_eval_fake_provider() -> None:
    settings = make_test_settings()
    cases = _datasets()
    llm = ScriptedGoldLLM(cases)
    report = await run_eval(settings=settings, llm=llm, cases=cases, normalization="strict")
    assert report["cases_total"] == len(cases)
    assert report["schema_pass_rate"] == 1.0
    assert report["first_pass_rate"] == 1.0
    # strict-нормализация меняет часть значений (даты→ISO) — точное
    # совпадение в fake-режиме < 1.0, это задокументировано в docs/EVAL.md
    assert report["field_exact_match_rate"] >= 0.9
    assert report["numeric_accuracy_rate"] == 1.0
    assert report["average_cost_credits"] > 0
    assert report["average_llm_calls"] == 1.0
    assert len(report["results"]) == len(cases)
    # отчёт самодостаточен: без сырых текстов
    assert "raw_text" not in json.dumps(report)


async def test_run_eval_with_failing_case() -> None:
    settings = make_test_settings()
    cases = _datasets()[:1]

    class AlwaysGarbage:
        async def complete(self, **kwargs):
            from app.llm import LLMResult

            return LLMResult(content="не JSON вообще", input_tokens=1, output_tokens=1)

    report = await run_eval(
        settings=settings, llm=AlwaysGarbage(), cases=cases, normalization="off"
    )
    assert report["schema_pass_rate"] == 0.0
    failed = report["results"][0]
    assert failed["error"] == "validation_failed"
    assert failed["attempts"] >= 1
