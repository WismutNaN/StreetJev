import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Signal

from streetjev.application.ports import GeoExpert
from streetjev.application.session import AnalysisResult
from streetjev.domain.evidence import Frame
from streetjev.domain.round import GeoRound

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    captured = Signal(object)
    eligible = Signal(object)
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
            if novelty > 0:
                self.signals.eligible.emit(AnalysisResult(self.round_state.id, frame, None, novelty))
            evidence = self.expert.infer(frame) if novelty > 0 else None
            self.signals.completed.emit(AnalysisResult(self.round_state.id, frame, evidence, novelty))
        except Exception as exc:
            log.exception("Ошибка фонового анализа")
            self.signals.failed.emit(self.round_state.id, str(exc))
        finally:
            self.signals.finished.emit()


class SupplementWorker:
    """Один необязательный HTTP-запрос; не удерживает GUI при закрытии."""

    def __init__(self, request: AnalysisResult, operation):
        self.request, self.operation = request, operation
        self.signals = WorkerSignals()

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="chipoint-http").start()

    def run(self):
        try:
            item = self.request
            evidence = self.operation(item.frame)
            self.signals.completed.emit(AnalysisResult(item.round_id, item.frame, evidence, item.novelty))
        except Exception as exc:
            log.exception("Необязательный эксперт не завершил запрос")
            try:
                self.signals.failed.emit(self.request.round_id, str(exc))
            except RuntimeError:
                pass
        finally:
            try:
                self.signals.finished.emit()
            except RuntimeError:
                pass
