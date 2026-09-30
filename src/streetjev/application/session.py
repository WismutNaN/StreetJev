from dataclasses import dataclass, replace

from streetjev.application.ports import RoundRepository
from streetjev.domain.calibration import CalibrationProfile
from streetjev.domain.evidence import Frame, GeoEvidence
from streetjev.domain.fusion import fuse_evidence
from streetjev.domain.geography import GeoCatalog
from streetjev.domain.ground_truth import GroundTruth
from streetjev.domain.round import GeoRound


@dataclass(frozen=True)
class AnalysisResult:
    round_id: str
    frame: Frame
    evidence: GeoEvidence | None
    novelty: float


class Session:
    """Вызывается в UI-потоке; inference выполняется отдельно и возвращает результат."""

    def __init__(self, catalog: GeoCatalog, repository: RoundRepository, profile=None):
        self.catalog = catalog
        self.repository = repository
        self.profile = profile or CalibrationProfile()
        self.round = GeoRound(catalog)
        self.repository.start(self.round.snapshot())
        self._observations: dict[str, AnalysisResult] = {}
        self._supplements: dict[str, GeoEvidence] = {}
        self._received_supplements: set[str] = set()
        self.truth = None

    def reset(self):
        next_round = GeoRound(self.catalog)
        self.repository.start(next_round.snapshot())
        self.round = next_round
        self._observations = {}
        self._supplements = {}
        self._received_supplements = set()
        self.truth = None

    def accept(self, result: AnalysisResult) -> bool:
        if result.evidence is None or result.round_id != self.round.id or self.truth is not None:
            return False
        # Сначала транзакция сохранения, затем публикация нового состояния.
        candidate = self.round.with_evidence(result.round_id, result.frame, result.evidence, result.novelty)
        if candidate is not None:
            self.repository.append(candidate.snapshot(), result.frame, result.evidence, result.novelty)
            self.round = candidate
            self._observations[result.frame.id] = replace(result, frame=replace(result.frame, jpeg=b""))
        return candidate is not None

    def label(self, truth: GroundTruth):
        if not self.round.frames:
            raise ValueError("Сначала проанализируйте хотя бы один кадр")
        if truth.country not in self.catalog.countries or (
            truth.region is not None
            and (
                truth.region not in self.catalog.regions
                or self.catalog.regions[truth.region].country_code != truth.country
            )
        ):
            raise ValueError("Страна и регион правильного ответа не согласованы")
        self.repository.label(self.round.id, truth)
        self.truth = truth

    def _rebuild(self, supplements, observations=None):
        state = GeoRound(self.catalog)
        state.id = self.round.id
        for item in (self._observations if observations is None else observations).values():
            evidence = item.evidence
            if item.frame.id in supplements:
                secondary = supplements[item.frame.id]
                evidence = fuse_evidence(
                    evidence,
                    secondary,
                    self.profile.fusion_weight(evidence.model_version, secondary.model_version),
                )
            state.accept(state.id, item.frame, evidence, item.novelty)
        return state

    def supplement(self, result: AnalysisResult) -> bool:
        """Поздний эксперт уточняет тот же кадр; число кадров не увеличивается."""
        if (
            result.round_id != self.round.id
            or result.evidence is None
            or result.frame.id not in self._observations
            or result.frame.id in self._received_supplements
        ):
            return False
        supplements = {**self._supplements, result.frame.id: result.evidence}
        candidate = self._rebuild(supplements)
        primary = self._observations[result.frame.id].evidence
        weight = self.profile.fusion_weight(primary.model_version, result.evidence.model_version)
        self.repository.supplement(candidate.snapshot(), result.frame, result.evidence, weight)
        self.round, self._supplements = candidate, supplements
        self._received_supplements.add(result.frame.id)
        return True

    def has_frame(self, frame_id):
        return frame_id in self._observations

    def observation(self, frame_id):
        return self._observations.get(frame_id)

    def refine(self, result: AnalysisResult) -> bool:
        """Дополняет imajev только новыми региональными ответами того же кадра."""
        previous = self._observations.get(result.frame.id)
        if result.round_id != self.round.id or previous is None or result.evidence is None:
            return False
        old, new = previous.evidence, result.evidence
        if (
            old.countries != new.countries
            or old.clues != new.clues
            or old.source != new.source
            or old.model_version != new.model_version
            or old.cells != new.cells
            or old.points != new.points
            or not set(new.regions) - set(old.regions)
            or any(new.regions.get(key) != value for key, value in old.regions.items())
        ):
            return False
        observations = {**self._observations, result.frame.id: replace(previous, evidence=new)}
        candidate = self._rebuild(self._supplements, observations)
        self.repository.refine(candidate.snapshot(), result.frame, new)
        self.round, self._observations = candidate, observations
        return True

    def clear_supplements(self):
        """Отключение возвращает накопленные ответы imajev, не сбрасывая раунд."""
        if self._supplements:
            candidate = self._rebuild({})
            self.repository.disable_supplements(candidate.snapshot())
            self.round, self._supplements = candidate, {}
