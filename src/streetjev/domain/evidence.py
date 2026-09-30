import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from streetjev.domain.localization import LocalCell, LocalPoint


@dataclass(frozen=True)
class Distribution:
    """Вероятности вариантов при условии, что ответ известен; unknown отдельно."""

    probabilities: Mapping[str, float]
    unknown: float = 0.0

    def __post_init__(self):
        values = dict(self.probabilities)
        if not values or any(not math.isfinite(p) or p < 0 for p in values.values()):
            raise ValueError("Некорректное распределение")
        if not math.isfinite(self.unknown) or not 0 <= self.unknown <= 1:
            raise ValueError("unknown должен быть в диапазоне [0, 1]")
        total = math.fsum(values.values())
        if total <= 0 or not math.isfinite(total):
            if self.unknown != 1:
                raise ValueError("Распределение не содержит вероятностной массы")
            values = {k: 1 / len(values) for k in values}
        else:
            values = {k: p / total for k, p in values.items()}
        object.__setattr__(self, "probabilities", MappingProxyType(values))


@dataclass(frozen=True)
class Frame:
    id: str
    jpeg: bytes
    fingerprint: bytes
    captured_at: str
    source: str


@dataclass(frozen=True)
class GeoEvidence:
    source: str
    model_version: str
    countries: Distribution
    regions: Mapping[str, Distribution]
    raw_json: str
    elapsed_ms: float
    clues: Mapping[str, Distribution] = field(default_factory=dict)
    cells: tuple[LocalCell, ...] = ()
    points: tuple[LocalPoint, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "regions", MappingProxyType(dict(self.regions)))
        object.__setattr__(self, "clues", MappingProxyType(dict(self.clues)))
        object.__setattr__(self, "cells", tuple(self.cells))
        object.__setattr__(self, "points", tuple(self.points))
        if math.fsum(c.mass for c in self.cells) > 1 + 1e-8:
            raise ValueError("Поддержка локальных зон превышает единицу")
        if math.fsum(p.mass for p in self.points) > 1 + 1e-8:
            raise ValueError("Поддержка локальных совпадений превышает единицу")
