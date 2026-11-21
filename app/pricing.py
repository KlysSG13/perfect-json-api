"""Тарификация в кредитах.

Правила:
- Кредиты — целые числа; итоговое списание округляется вверх (math.ceil).
- Стоимость = базовая_стоимость × число_вызовов_LLM
             + цена_входа × (символы/1000) × число_вызовов_LLM
             + цена_выхода × (токены/1000), суммарно по всем вызовам.
- До вызова LLM выполняется консервативная оценка (запас на ~1000 выходных
  токенов); по ней проверяется баланс.
- Если провайдер не вернул usage, выходные токены оцениваются как len/4.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.config import Settings

#: Консервативная оценка выходных токенов для pre-flight проверки баланса.
ESTIMATED_OUTPUT_TOKENS = 1000

#: Эмпирическая оценка: ~4 символа на токен, если usage недоступен.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class CostBreakdown:
    credits: int
    llm_calls: int
    input_chars: int
    output_tokens_estimated: int


class PricingService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def base_cost(self) -> int:
        return self._settings.credit_base_cost

    def estimate_cost(self, input_chars: int) -> int:
        """Оценка стоимости одной попытки до вызова LLM."""
        raw = (
            self._settings.credit_base_cost
            + (input_chars / 1000.0) * self._settings.credit_cost_per_1k_input_chars
            + (ESTIMATED_OUTPUT_TOKENS / 1000.0) * self._settings.credit_cost_per_1k_output_tokens
        )
        return int(math.ceil(raw))

    def max_cost(self, input_chars: int, *, llm_calls: int) -> int:
        """Консервативная максимальная стоимость запроса.

        Учитывает до llm_calls вызовов (max_repairs + 1), вход каждый раз
        и максимум выходных токенов LLM_MAX_OUTPUT_TOKENS на вызов.
        """
        calls = max(1, llm_calls)
        raw = (
            self._settings.credit_base_cost * calls
            + (input_chars / 1000.0) * self._settings.credit_cost_per_1k_input_chars * calls
            + (
                (self._settings.llm_max_output_tokens * calls) / 1000.0
                * self._settings.credit_cost_per_1k_output_tokens
            )
        )
        return int(math.ceil(raw))

    def single_call_cost(self, input_chars: int) -> int:
        """Стоимость одного дополнительного вызова (для контроля лимита при автопочинке)."""
        raw = (
            self._settings.credit_base_cost
            + (input_chars / 1000.0) * self._settings.credit_cost_per_1k_input_chars
            + (
                (self._settings.llm_max_output_tokens / 1000.0)
                * self._settings.credit_cost_per_1k_output_tokens
            )
        )
        return int(math.ceil(raw))

    def compute_cost(
        self,
        *,
        input_chars: int,
        llm_calls: int,
        outputs: list[tuple[int, int | None]],
    ) -> CostBreakdown:
        """Фактическая стоимость после обработки.

        outputs — список (длина_ответа_в_символах, output_tokens | None)
        по одному элементу на каждый вызов LLM.
        """
        output_tokens_total = sum(
            tokens if tokens is not None else max(1, chars // CHARS_PER_TOKEN)
            for chars, tokens in outputs
        )
        raw = (
            self._settings.credit_base_cost * llm_calls
            + (input_chars / 1000.0) * self._settings.credit_cost_per_1k_input_chars * llm_calls
            + (output_tokens_total / 1000.0) * self._settings.credit_cost_per_1k_output_tokens
        )
        return CostBreakdown(
            credits=int(math.ceil(raw)),
            llm_calls=llm_calls,
            input_chars=input_chars,
            output_tokens_estimated=output_tokens_total,
        )

    def estimated_output_tokens_for(self, content_chars: int) -> int:
        return max(1, content_chars // CHARS_PER_TOKEN)
