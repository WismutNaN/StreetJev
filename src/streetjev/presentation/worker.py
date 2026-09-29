import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Signal

from streetjev.application.ports import GeoExpert
from streetjev.application.session import AnalysisResult
from streetjev.domain.evidence import Frame
from streetjev.domain.round import GeoRound

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    captured = Signal(object)
    completed = Signal(object)
    failed = Signal(str, str)
    finished = Signal()


class AnalysisWorker(QRunnable):
    def __init__(self, round_state: GeoRound, capture: Callable[[], Frame], expert: GeoExpert):
        super().__init__()
        self.round_state, self.capture, self.expert = round_state, capture, expert
        self.signals = WorkerSignals()

    def run(self):
        try:
            frame = self.capture()
            self.signals.captured.emit(frame)
            novelty = self.round_state.novelty(frame)
            evidence = self.expert.infer(frame) if novelty > 0 else None
            self.signals.completed.emit(AnalysisResult(self.round_state.id, frame, evidence, novelty))
        except Exception as exc:
            log.exception("Ошибка фонового анализа")
            self.signals.failed.emit(self.round_state.id, str(exc))
        finally:
            self.signals.finished.emit()
