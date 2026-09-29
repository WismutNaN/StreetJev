from dataclasses import dataclass

from streetjev.application.ports import RoundRepository
from streetjev.domain.evidence import Frame, GeoEvidence
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

    def reset(self):
        next_round = GeoRound(self.catalog)
        self.repository.start(next_round.snapshot())
        self.round = next_round

    def accept(self, result: AnalysisResult) -> bool:
        if result.evidence is None or result.round_id != self.round.id:
            return False
        # Сначала транзакция сохранения, затем публикация нового состояния.
        candidate = self.round.with_evidence(result.round_id, result.frame, result.evidence, result.novelty)
        if candidate is not None:
            self.repository.append(candidate.snapshot(), result.frame, result.evidence, result.novelty)
            self.round = candidate
        return candidate is not None
