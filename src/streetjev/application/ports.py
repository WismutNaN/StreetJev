from typing import Protocol

from streetjev.domain.evidence import Frame, GeoEvidence
from streetjev.domain.round import RoundSnapshot


class GeoExpert(Protocol):
    def infer(self, frame: Frame) -> GeoEvidence: ...


class FrameCapture(Protocol):
    def capture(self) -> Frame: ...


class RoundRepository(Protocol):
    def start(self, snapshot: RoundSnapshot) -> None: ...

    def append(self, snapshot: RoundSnapshot, frame: Frame, evidence: GeoEvidence, novelty: float) -> None: ...
