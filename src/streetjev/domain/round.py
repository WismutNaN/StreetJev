import math
from copy import copy
from dataclasses import dataclass
from uuid import uuid4

from streetjev.domain.evidence import Distribution, Frame, GeoEvidence
from streetjev.domain.geography import GeoCatalog


def update_posterior(previous: dict[str, float], evidence: Distribution, weight: float) -> dict[str, float]:
    """Log-space Bayes, равномерный prior; неизвестный ответ ослабляет вес."""
    if set(previous) != set(evidence.probabilities):
        raise ValueError("Набор гипотез изменился внутри раунда")
    strength = weight * (1 - evidence.unknown)
    if strength == 0:
        return previous.copy()
    size = len(previous)
    scores = {
        key: math.log(max(value, 1e-300)) + strength * math.log(max(evidence.probabilities[key], 1e-8) * size)
        for key, value in previous.items()
    }
    maximum = max(scores.values())
    exp = {key: math.exp(value - maximum) for key, value in scores.items()}
    total = math.fsum(exp.values())
    return {key: value / total for key, value in exp.items()}


@dataclass(frozen=True)
class RoundSnapshot:
    id: str
    frames: int
    effective_frames: float
    countries: dict[str, float]
    regions: dict[str, float]
    observed_regions: frozenset[str]
    last_unknown: float


class GeoRound:
    """Агрегат: один раунд владеет всеми накопленными гипотезами."""

    def __init__(self, catalog: GeoCatalog):
        self.catalog = catalog
        self.id = uuid4().hex
        self.countries = {key: 1 / len(catalog.countries) for key in catalog.countries}
        self.region_conditionals = {
            key: {p.code: 1 / len(regions) for p in regions}
            for key in catalog.countries
            if (regions := catalog.regions_of(key))
        }
        self.observed_regions: set[str] = set()
        self.frames = 0
        self.effective_frames = 0.0
        self.last_unknown = 1.0
        self._seen_ids: set[str] = set()
        self._fingerprints: list[bytes] = []
        self._seen_fingerprints: set[bytes] = set()

    def novelty(self, frame: Frame) -> float:
        if not frame.fingerprint or frame.fingerprint in self._seen_fingerprints:
            return 0.0
        if not self._fingerprints:
            return 1.0
        if any(len(previous) != len(frame.fingerprint) for previous in self._fingerprints):
            raise ValueError("Изменился формат отпечатка кадра")
        distance = min(
            sum(abs(a - b) for a, b in zip(frame.fingerprint, previous, strict=True)) / (255 * len(previous))
            for previous in self._fingerprints
        )
        # Эвристика MVP: почти одинаковые кадры пропускаются, новый ракурс весит до 1.
        return min(1.0, max(0.0, (distance - 0.025) / 0.15))

    def accept(self, round_id: str, frame: Frame, evidence: GeoEvidence, novelty: float) -> bool:
        if round_id != self.id or frame.id in self._seen_ids or frame.fingerprint in self._seen_fingerprints:
            return False
        if not math.isfinite(novelty) or not 0 <= novelty <= 1:
            raise ValueError("Вес новизны должен быть в диапазоне [0, 1]")
        if novelty == 0:
            return False
        # Вычисляем всё до изменения агрегата: неверный регион не оставит частичный update.
        countries = update_posterior(self.countries, evidence.countries, novelty)
        conditionals = self.region_conditionals.copy()
        observed = self.observed_regions.copy()
        for country, distribution in evidence.regions.items():
            if country not in conditionals:
                raise ValueError("Неизвестная страна в региональном наблюдении")
            # Региональный ответ условный: слабая гипотеза страны не должна закреплять регион.
            weight = novelty * (1 - evidence.countries.unknown) * evidence.countries.probabilities[country]
            conditionals[country] = update_posterior(conditionals[country], distribution, weight)
            if weight * (1 - distribution.unknown) > 0:
                observed.add(country)
        self.countries = countries
        self.region_conditionals = conditionals
        self.observed_regions = observed
        self.frames += 1
        self.effective_frames += novelty * (1 - evidence.countries.unknown)
        self.last_unknown = evidence.countries.unknown
        self._seen_ids.add(frame.id)
        self._seen_fingerprints.add(frame.fingerprint)
        self._fingerprints.append(frame.fingerprint)
        self._fingerprints = self._fingerprints[-256:]
        return True

    def with_evidence(self, round_id: str, frame: Frame, evidence: GeoEvidence, novelty: float):
        """Кандидат на новое состояние для атомарного сохранения, без изменения текущего."""
        candidate = copy(self)
        candidate._seen_ids = self._seen_ids.copy()
        candidate._fingerprints = self._fingerprints.copy()
        candidate._seen_fingerprints = self._seen_fingerprints.copy()
        return candidate if candidate.accept(round_id, frame, evidence, novelty) else None

    def snapshot(self) -> RoundSnapshot:
        return RoundSnapshot(
            self.id,
            self.frames,
            self.effective_frames,
            self.countries.copy(),
            {
                region: self.countries[country] * probability
                for country, regions in self.region_conditionals.items()
                for region, probability in regions.items()
            },
            frozenset(self.observed_regions),
            self.last_unknown,
        )
