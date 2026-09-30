import math
from copy import copy
from dataclasses import dataclass, field, replace
from uuid import uuid4

from streetjev.domain.evidence import Distribution, Frame, GeoEvidence
from streetjev.domain.geography import GeoCatalog
from streetjev.domain.localization import LocalCell, combine_cells, local_scores

AGGREGATION_VERSION = "weighted_linear_v2"


def pool_scores(
    previous: dict[str, float], previous_weight: float, evidence: Distribution, weight: float
) -> dict[str, float]:
    """Линейное объединение: результат лежит между оценками отдельных кадров."""
    if set(previous) != set(evidence.probabilities):
        raise ValueError("Набор гипотез изменился внутри раунда")
    if not math.isfinite(previous_weight) or previous_weight < 0 or not math.isfinite(weight) or weight < 0:
        raise ValueError("Некорректный вес наблюдения")
    strength = weight * (1 - evidence.unknown)
    if strength == 0:
        return previous.copy()
    fraction = strength / (previous_weight + strength)
    return {
        key: (1 - fraction) * value + fraction * evidence.probabilities[key] for key, value in previous.items()
    }


@dataclass(frozen=True)
class RoundSnapshot:
    id: str
    frames: int
    effective_frames: float
    countries: dict[str, float]
    regions: dict[str, float]
    observed_regions: frozenset[str]
    last_unknown: float
    aggregation: str = AGGREGATION_VERSION
    last_countries: dict[str, float] = field(default_factory=dict)
    last_regions: dict[str, float] = field(default_factory=dict)
    last_observed_regions: frozenset[str] = frozenset()
    known_frames: int = 0
    agreeing_frames: int = 0
    cells: tuple[LocalCell, ...] = ()
    last_cells: tuple[LocalCell, ...] = ()


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
        self._region_weights = {key: 0.0 for key in self.region_conditionals}
        self._last_evidence: GeoEvidence | None = None
        self._leader_votes = {key: 0 for key in catalog.countries}
        self.known_frames = 0
        self.frames = 0
        self.effective_frames = 0.0
        self.last_unknown = 1.0
        self._seen_ids: set[str] = set()
        self._fingerprints: list[bytes] = []
        self._seen_fingerprints: set[bytes] = set()
        self._cells: tuple[LocalCell, ...] = ()
        self._local_weight = 0.0

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
        countries = pool_scores(self.countries, self.effective_frames, evidence.countries, novelty)
        conditionals = self.region_conditionals.copy()
        observed = self.observed_regions.copy()
        region_weights = self._region_weights.copy()
        for country, distribution in evidence.regions.items():
            if country not in conditionals:
                raise ValueError("Неизвестная страна в региональном наблюдении")
            # Региональный ответ условный: слабая гипотеза страны не должна закреплять регион.
            weight = novelty * (1 - evidence.countries.unknown) * evidence.countries.probabilities[country]
            conditionals[country] = pool_scores(
                conditionals[country], region_weights[country], distribution, weight
            )
            region_weights[country] += weight * (1 - distribution.unknown)
            if weight * (1 - distribution.unknown) > 0:
                observed.add(country)
        for cell in evidence.cells:
            if cell.country not in self.catalog.countries or (
                cell.region
                and (
                    cell.region not in self.catalog.regions
                    or self.catalog.regions[cell.region].country_code != cell.country
                )
            ):
                raise ValueError("Неизвестный родитель локальной зоны")
        cells = combine_cells((*self._cells, *(replace(c, mass=c.mass * novelty) for c in evidence.cells)))
        self._cells = cells
        self._local_weight += novelty
        self.countries = countries
        self.region_conditionals = conditionals
        self.observed_regions = observed
        self._region_weights = region_weights
        self._last_evidence = evidence
        self._leader_votes = self._leader_votes.copy()
        if evidence.countries.unknown < 1:
            self.known_frames += 1
            ranked = sorted(evidence.countries.probabilities.items(), key=lambda pair: pair[1], reverse=True)
            if len(ranked) == 1 or ranked[0][1] - ranked[1][1] > 1e-12:
                self._leader_votes[ranked[0][0]] += 1
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
        last = self._last_evidence
        regions = {
            region: self.countries[country] * probability
            for country, values in self.region_conditionals.items()
            for region, probability in values.items()
        }
        last_regions = (
            {
                region: last.countries.probabilities[country] * probability
                for country, distribution in last.regions.items()
                for region, probability in distribution.probabilities.items()
            }
            if last
            else {}
        )
        return RoundSnapshot(
            self.id,
            self.frames,
            self.effective_frames,
            self.countries.copy(),
            regions,
            frozenset(self.observed_regions),
            self.last_unknown,
            last_countries=dict(last.countries.probabilities) if last else {},
            last_regions=last_regions,
            last_observed_regions=frozenset(
                country
                for country, distribution in last.regions.items()
                if distribution.unknown < 1 and last.countries.unknown < 1
            )
            if last
            else frozenset(),
            known_frames=self.known_frames,
            agreeing_frames=self._leader_votes[max(self.countries, key=self.countries.get)]
            if self.known_frames
            else 0,
            cells=local_scores(self._cells, self._local_weight, self.countries, regions),
            last_cells=local_scores(last.cells, 1, last.countries.probabilities, last_regions) if last else (),
        )
