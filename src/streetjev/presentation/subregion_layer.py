"""Подрегионы загружаются в фоне; в сцене находятся только видимые районы."""

import logging
import time
from collections import OrderedDict

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QLabel

from streetjev.domain.subregions import subregion_support
from streetjev.infrastructure.subregions import SubregionRepository
from streetjev.presentation.map_geometry import project, unproject
from streetjev.presentation.map_polygon import MapPolygon
from streetjev.presentation.map_scale import HeatScale, format_score, support_color
from streetjev.presentation.scores import display_scores

log = logging.getLogger(__name__)


class Loaded(QObject):
    done = Signal(str, object, str)


class LoadBoundaries(QRunnable):
    def __init__(self, repository, country, signals):
        super().__init__()
        self.repository, self.country, self.signals = repository, country, signals

    def run(self):
        try:
            dataset = self.repository.load(self.country)
            self.signals.done.emit(self.country, dataset, "")
        except Exception as exc:
            log.exception("Ошибка подготовки ADM2 %s", self.country)
            self.signals.done.emit(self.country, None, str(exc))


class SubregionLayer(QObject):
    changed = Signal()

    def __init__(self, view, cache_dir, geography, enabled=True):
        super().__init__(view)
        self.view, self.enabled = view, enabled
        self.closed = False
        self.repository = SubregionRepository(cache_dir, geography)
        self.datasets, self.path_cache = OrderedDict(), OrderedDict()
        self.items, self.pending, self.retry_at = {}, set(), {}
        self.dimmed = set()
        self.errors = {}
        self.places, self.support, self.assignments = {}, {}, {}
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.signals = Loaded(self)
        self.signals.done.connect(self.received)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.refresh)
        self.credit = QLabel(
            '<a style="color:#87a6b9" href="https://www.geoboundaries.org/">ADM2 · geoBoundaries</a>',
            view.viewport(),
        )
        self.credit.setOpenExternalLinks(True)
        self.credit.setStyleSheet("background:#0f1c28;padding:3px;font-size:10px")
        self.credit.hide()
        view.viewport_changed.connect(self.schedule)
        view.snapshot_changed.connect(self.updated)
        view.subregions = self

    def schedule(self):
        if not self.closed:
            self.timer.start()
        self.credit.adjustSize()
        self.credit.move(
            max(0, self.view.viewport().width() - self.credit.width() - 6),
            max(0, self.view.viewport().height() - self.credit.height() - 31),
        )

    def updated(self):
        self.recalculate()
        # Старые оценки не видны во время debounce, в том числе после сброса.
        self.restyle()
        self.schedule()

    def projection(self, scores):
        assignments = {}
        for point in scores.points if scores.active else ():
            dataset = self.datasets.get(point.country)
            if dataset:
                code = dataset.locate(point.latitude, point.longitude)
                if code:
                    assignments[point.code] = code
        return subregion_support(
            scores.points if scores.active else (), assignments, self.places, scores.countries, scores.regions
        ), assignments

    def recalculate(self):
        self.places = {p.code: p for d in self.datasets.values() for p in d.places}
        if self.view.snapshot:
            self.support, self.assignments = self.projection(display_scores(self.view.snapshot, self.view.source))

    def refresh(self):
        if self.closed or not self.enabled or not self.view.isVisible():
            return
        view = self.view
        rectangle = view.mapToScene(view.viewport().rect()).boundingRect()
        scale = view.transform().m11()
        desired = []
        if view.snapshot:
            scores = display_scores(view.snapshot, view.source)
            if scores.active:
                desired = sorted(scores.countries, key=scores.countries.get, reverse=True)[:3]
                desired = [c for c in desired if scores.countries[c] > 0.01]
        if scale >= 48:
            nearby = [
                c
                for c, bounds in view.country_bounds.items()
                if bounds.intersects(rectangle) and view.items_by_code[c].path().intersects(rectangle)
            ]
            nearby.sort(
                key=lambda c: (
                    view.country_bounds[c].intersected(rectangle).width()
                    * view.country_bounds[c].intersected(rectangle).height()
                ),
                reverse=True,
            )
            desired = list(dict.fromkeys([*nearby[:3], *desired]))
        desired = [c for c in desired if c in self.repository.iso3]
        for country in desired:
            if country in self.datasets:
                self.datasets.move_to_end(country)
            elif country not in self.pending and self.retry_at.get(country, 0) < time.monotonic():
                if len(self.pending) >= 2:
                    continue
                self.pending.add(country)
                self.pool.start(LoadBoundaries(self.repository, country, self.signals))
        wanted = {}
        if scale >= 48:
            left, north = unproject(max(-180, rectangle.left()), rectangle.top())
            right, south = unproject(min(180, rectangle.right()), rectangle.bottom())
            lod = 0 if scale < 180 else 1 if scale < 900 else 2
            for country in desired:
                dataset = self.datasets.get(country)
                if dataset and left < right:
                    for i in dataset.visible((left, south, right, north)):
                        wanted[dataset.places[i].code] = (dataset, lod)
        # Очень крупная страна не должна создавать тысячи Qt items в обзорном масштабе.
        # В таком виде показываем только поддержанные районы, остальные границы — при приближении.
        if len(wanted) > 240:
            wanted = {code: value for code, value in wanted.items() if self.support.get(code, 0) > 0}
        wanted = dict(sorted(wanted.items(), key=lambda e: self.support.get(e[0], 0), reverse=True)[:240])
        for code in list(self.items):
            if code not in wanted or self.items[code].data(2) != wanted[code][1]:
                view.scene().removeItem(self.items.pop(code))
        for code, (dataset, lod) in wanted.items():
            if code in self.items:
                continue
            path = self.path(code, dataset, lod)
            item = MapPolygon(path)
            item.setData(0, code)
            item.setData(1, dataset.country)
            item.setData(2, lod)
            item.setZValue(1.5)
            item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            view.scene().addItem(item)
            self.items[code] = item
        self.restyle()
        self.credit.setVisible(bool(self.items) or scale >= 48 and bool(desired))
        suffix = " · загрузка…" if any(c in self.pending for c in desired) else ""
        unavailable = [c for c in desired if c not in self.datasets and c in self.retry_at]
        if unavailable:
            suffix += " · недоступно: " + ", ".join(unavailable)
        self.credit.setText(
            '<a style="color:#87a6b9" href="https://www.geoboundaries.org/">ADM2 · geoBoundaries</a>' + suffix
        )
        licenses = [
            f"{d.country}: {d.metadata.get('boundaryYearRepresented', '?')} · "
            f"{d.metadata.get('boundaryLicense', '')} · {d.metadata.get('boundarySource', '')}"
            for c, d in self.datasets.items()
            if c in desired
        ]
        licenses.extend(f"{c}: {self.errors.get(c, '')}" for c in unavailable)
        self.credit.setToolTip("\n".join(licenses) or "Подрегионы загружаются по мере приближения")
        self.schedule_credit()

    def schedule_credit(self):
        self.credit.adjustSize()
        self.credit.move(
            max(0, self.view.viewport().width() - self.credit.width() - 6),
            max(0, self.view.viewport().height() - self.credit.height() - 31),
        )
        self.credit.raise_()

    def path(self, code, dataset, lod):
        key = code, lod
        if key not in self.path_cache:
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.OddEvenFill)
            for polygon in dataset.drawings[code][lod]:
                for ring in polygon:
                    if not ring:
                        continue
                    path.moveTo(*project(*ring[0]))
                    previous = ring[0][0]
                    for lon, lat in ring[1:]:
                        if abs(lon - previous) > 180:
                            path.moveTo(*project(lon, lat))
                        else:
                            path.lineTo(*project(lon, lat))
                        previous = lon
                    path.closeSubpath()
            self.path_cache[key] = path
        self.path_cache.move_to_end(key)
        while len(self.path_cache) > 600:
            self.path_cache.popitem(last=False)
        return self.path_cache[key]

    def restyle(self):
        scale = HeatScale.from_scores((self.support.get(c, 0) for c in self.places), self.view.scale_mode)
        dimmed = {self.places[c].region for c in self.items if c in self.places and self.support.get(c, 0) > 0}
        if self.view.snapshot:
            scores = display_scores(self.view.snapshot, self.view.source)
            for code in self.dimmed | dimmed:
                if code not in self.view.items_by_code:
                    continue
                item = self.view.items_by_code[code]
                color = item.brush().color()
                color.setAlphaF(
                    (0.14 if code in dimmed else 0.42)
                    if scores.active and scores.regions.get(code, 0) > 0 and not self.view.answer_mode
                    else 0
                )
                item.setBrush(color)
        self.dimmed = dimmed
        labels = []
        for code, item in self.items.items():
            place = self.places.get(code)
            if place is None:
                item.hide()
                continue
            item.show()
            mass = self.support.get(code, 0)
            color = QColor(Qt.GlobalColor.transparent)
            if mass > 0 and not self.view.answer_mode:
                heat = support_color(scale.intensity(mass))
                base = QColor("#1a2d3b")
                color = QColor(*(round(a * 0.52 + b * 0.48) for a, b in zip(heat.getRgb()[:3], base.getRgb()[:3])))
            item.setBrush(color)
            pen = QPen(QColor(150, 166, 177, 100), 0.55)
            pen.setCosmetic(True)
            item.setPen(pen)
            parent = self.view.catalog.regions.get(place.region)
            item.setToolTip(
                f"{self.view.catalog.countries[place.country].label} · "
                f"{parent.label + ' · ' if parent else ''}{place.name}\n"
                + (
                    f"Поддержка ADM2: {format_score(mass)} · по точкам Chipoint"
                    if mass > 0
                    else "ADM2 · нет координатных свидетельств"
                )
                + "\nГраницы geoBoundaries; поддержка не равна вероятности правильного ответа"
            )
            bounds = item.boundingRect()
            if mass > 0 and not self.view.answer_mode and bounds.width() * self.view.transform().m11() > 140:
                dataset = self.datasets[place.country]
                x, y = project(*dataset.anchors[code])
                labels.append((x, y, place.name, mass, "subregion"))
        self.view.subregion_labels = sorted(labels, key=lambda label: label[3], reverse=True)[:6]
        self.view._label_key = None
        self.view.viewport().update()

    def received(self, country, dataset, error):
        self.pending.discard(country)
        if self.closed:
            return
        if dataset is not None:
            self.datasets[country] = dataset
            while len(self.datasets) > 8:
                self.datasets.popitem(last=False)
            self.recalculate()
            self.changed.emit()
        else:
            self.retry_at[country] = time.monotonic() + 300
            self.errors[country] = error
            log.info("ADM2 %s недоступен: %s", country, error)
        self.refresh()

    def focus(self, code):
        place = self.places.get(code)
        if place:
            self.view.auto_fit = False
            bounds = self.path(code, self.datasets[place.country], 1).boundingRect()
            margin = max(bounds.width(), bounds.height()) * 0.15 + 0.005
            self.view.fitInView(
                bounds.adjusted(-margin, -margin, margin, margin), Qt.AspectRatioMode.KeepAspectRatio
            )

    def close(self):
        self.closed = True
        self.timer.stop()
        self.pool.clear()
