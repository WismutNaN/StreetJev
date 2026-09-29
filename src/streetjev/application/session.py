from dataclasses import dataclass, replace

from streetjev.application.ports import RoundRepository
from streetjev.domain.evidence import Frame, GeoEvidence
from streetjev.domain.fusion import fuse_evidence
from streetjev.domain.geography import GeoCatalog
from streetjev.domain.round import GeoRound


@dataclass(frozen=True)
class AnalysisResult:
    round_id: str
    frame: Frame
    evidence: GeoEvidence | None
    novelty: float


class Session:
    """Вызывается в UI-потоке; inference выполняется отдельно и возвращает результат."""

    def __init__(self, catalog: GeoCatalog, repository: RoundRepository):
        self.catalog = catalog
        self.repository = repository
        self.round = GeoRound(catalog)
        self.repository.start(self.round.snapshot())
        self._observations: dict[str, AnalysisResult] = {}
        self._supplements: dict[str, GeoEvidence] = {}
        self._received_supplements: set[str] = set()
        self._secondary_weight = 0.35

    def reset(self):
        next_round = GeoRound(self.catalog)
        self.repository.start(next_round.snapshot())
        self.round = next_round
        self._observations = {}
        self._supplements = {}
        self._received_supplements = set()

    def accept(self, result: AnalysisResult) -> bool:
        if result.evidence is None or result.round_id != self.round.id:
            return False
        # Сначала транзакция сохранения, затем публикация нового состояния.
        candidate = self.round.with_evidence(result.round_id, result.frame, result.evidence, result.novelty)
        if candidate is not None:
            self.repository.append(candidate.snapshot(), result.frame, result.evidence, result.novelty)
            self.round = candidate
            self._observations[result.frame.id] = replace(result, frame=replace(result.frame, jpeg=b""))
        return candidate is not None

    def _rebuild(self, supplements):
        state = GeoRound(self.catalog)
        state.id = self.round.id
        for item in self._observations.values():
            evidence = item.evidence
            if item.frame.id in supplements:
                evidence = fuse_evidence(evidence, supplements[item.frame.id], self._secondary_weight)
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
        self.repository.supplement(candidate.snapshot(), result.frame, result.evidence, self._secondary_weight)
        self.round, self._supplements = candidate, supplements
        self._received_supplements.add(result.frame.id)
        return True

    def has_frame(self, frame_id):
        return frame_id in self._observations

    def clear_supplements(self):
        """Отключение возвращает накопленные ответы imajev, не сбрасывая раунд."""
        if self._supplements:
            candidate = self._rebuild({})
            self.repository.disable_supplements(candidate.snapshot())
            self.round, self._supplements = candidate, {}
