# Eval: измерение качества извлечения

## Расположение
- Датасеты: `app/eval/datasets/*.json` — 60 кейсов: 5 пресетов × 5, нормализация дат/денег/телефонов, adversarial-инъекции, противоречия, пропуски данных; кейс может задавать кастомную `schema` вместо пресета.
- Раннер: `app/eval/run.py`; отчёты: `eval/reports/` (`EVAL_REPORTS_DIR`).

## Запуск
```bash
python -m app.eval.run --provider fake            # офлайн: пайплайн + валидность золота
python -m app.eval.run --provider live            # реальный LLM (нужен LLM_API_KEY)
python -m app.eval.run --provider live --normalization strict
```
`--provider fake` отвечает «золотым» JSON каждого кейса: проверяет, что
gold проходит схемы и весь контур (валидация → нормализация → биллинг)
работает без сети. Качество модели измеряет только `live`.

## Структура кейса
```json
{
  "name": "invoice-basic-ru",
  "preset": "invoice_v1",
  "raw_text": "СЧЁТ №128 ...",
  "expected": { ...полный золотой JSON... },
  "expected_fields": {"total_amount": 50000}   # проверяемые поля
}
```
Датасеты синтетические, без персональных данных (проверяется тестом);
включают кейс с prompt injection (`universal-prompt-injection`).

## Метрики отчёта
| Метрика | Что означает |
|---|---|
| `schema_pass_rate` | доля финально валидных по схеме ответов |
| `first_pass_rate` | валидных с первой попытки (без автопочинки) |
| `repair_success_rate` | успешных автопоинок / потребовавших починки |
| `field_exact_match_rate` | точное совпадение `expected_fields` |
| `numeric_accuracy_rate` | совпадение числовых полей |
| `date_normalization_rate` | дат, приведённых к ISO (в режиме normal.) |
| `currency_normalization_rate` | валют, приведённых к ISO 4217 |
| `hallucination_flag_rate` | доля полей без следа во входе (эвристика) |
| `average_cost_credits` / `average_llm_calls` / `average_latency_ms` | экономика и скорость |

Значения `null` = метрика неприменима (например, `repair_success_rate`
в fake-режиме, где починка не требуется).

## Интерпретация
- fake-режим: все метрики качества = 1.0 по построению; важны
  `schema_pass_rate` (валидность золота) и отсутствие ошибок пайплайна.
- live-режим: сравнивайте отчёты между моделями/промптами; при смене
  `PROMPT_VERSION` сохраняйте оба отчёта для A/B.
- `hallucination_flag_rate` — эвристика (подстрочный поиск значения во
  входе); высокое значение — сигнал проверить экстракцию, не приговор.

## CI
`pytest tests/test_eval.py` — офлайн-гарантии: датасеты на месте, золото
валидно, раннер считает метрики, падение кейса фиксируется корректно.
Live-прогоны — вручную (кредиты!).


## Как добавлять кейсы
1. JSON-файл в `app/eval/datasets/`: список кейсов с `name`, `raw_text`,
   `expected`, `expected_fields`; `preset` ИЛИ `schema` (кастомная JSON Schema).
2. Gold обязан проходить схему (проверит `validate_gold` и тест
   `test_gold_passes_schemas`), без персональных данных.
3. `pytest tests/test_eval.py` → `python -m app.eval.run --provider fake`
   → сравнить метрики с предыдущим отчётом в `eval/reports/`.

## Live-eval
Требует `LLM_API_KEY`; не запускается в pytest. Команда:
`python -m app.eval.run --provider live [--normalization strict]`.
Перед живым прогоном зафиксируйте бюджет (кредиты тратятся реально).
