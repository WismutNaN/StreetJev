import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


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

    def __post_init__(self):
        object.__setattr__(self, "regions", MappingProxyType(dict(self.regions)))
