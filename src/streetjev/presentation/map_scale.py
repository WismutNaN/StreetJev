"""Контраст карты — преобразование для отрисовки, не изменение оценок домена."""

import math
from collections.abc import Iterable
from dataclasses import dataclass


def format_score(value: float) -> str:
    """Малые оценки не превращаются в 0%, почти единичные — в ложные 100%."""
    if value == 0:
        return "0%"
    if value < 0.00001:
        return "<0,001%"
    if value > 0.999:
        return ">99,9%"
    digits = 3 if value < 0.001 else 2 if value < 0.1 else 1
    return f"{100 * value:.{digits}f}%".replace(".", ",")


@dataclass(frozen=True)
class HeatScale:
    mode: str
    minimum: float
    maximum: float
    empty: bool = False

    @classmethod
    def from_scores(cls, scores: Iterable[float], mode: str = "relative"):
        if mode not in ("relative", "absolute"):
            raise ValueError("Неизвестная шкала карты")
        values = tuple(scores)
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
            raise ValueError("Оценки карты должны быть конечными числами из [0, 1]")
        if not values:
            return cls(mode, 0, 0, empty=True)
        if mode == "absolute":
            return cls(mode, 0, 1)
        return cls(mode, min(values), max(values))

    @property
    def flat(self) -> bool:
        return math.isclose(self.maximum, self.minimum, rel_tol=1e-6, abs_tol=1e-12)

    def intensity(self, value: float) -> float:
        if self.empty or self.flat:
            return 0
        fraction = min(1.0, max(0.0, (value - self.minimum) / (self.maximum - self.minimum)))
        return math.log1p(9 * fraction) / math.log(10) if self.mode == "relative" else fraction

    def value_at(self, intensity: float) -> float:
        fraction = math.expm1(intensity * math.log(10)) / 9 if self.mode == "relative" else intensity
        return self.minimum + (self.maximum - self.minimum) * fraction

    def legend(self) -> list[tuple[float, str]]:
        if self.empty:
            return [(0, "Нет наблюдений")]
        if self.flat:
            return [(0, f"Равные оценки: {format_score(self.minimum)}")]
        return [(t, format_score(self.value_at(t))) for t in (0, 0.2, 0.4, 0.6, 0.8, 1)]
