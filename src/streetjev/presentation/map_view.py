import math
from itertools import pairwise

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsView

from streetjev.domain.geography import GeoCatalog
from streetjev.domain.round import RoundSnapshot
from streetjev.presentation.auto_focus import Bounds, focus_bounds
from streetjev.presentation.map_scale import HeatScale, format_score
from streetjev.presentation.scores import display_scores


def project(lon, lat):
    lat = min(84, max(-84, lat))
    return lon, -math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


def unproject(x, y):
    return x, math.degrees(2 * math.atan(math.exp(math.radians(-y))) - math.pi / 2)


def heat_color(intensity):
    # Палитра общая; преобразование реальной оценки в интенсивность выполняет HeatScale.
    stops = [
        (0, (25, 42, 55)),
        (0.02, (34, 70, 92)),
        (0.1, (27, 139, 138)),
        (0.4, (80, 216, 168)),
        (0.7, (237, 203, 98)),
        (1, (251, 111, 80)),
    ]
    for (a, ca), (b, cb) in pairwise(stops):
        if intensity <= b:
            t = max(0, (intensity - a) / (b - a))
            return QColor(*(round(x + (y - x) * t) for x, y in zip(ca, cb)))
    return QColor(*stops[-1][1])


class MapView(QGraphicsView):
    viewport_changed = Signal()
    answer_clicked = Signal(float, float)
    country_clicked = Signal(str)
    scale_changed = Signal(object)
    manual_navigation = Signal()

    def __init__(self, catalog: GeoCatalog, geography: dict):
        super().__init__()
        self.catalog = catalog
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#0b1620"))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.items_by_code = {}
        self.country_bounds = {}
        self.level = "countries"
        self.source = "round"
        self.scale_mode = "relative"
        self.heat_scale = HeatScale.from_scores([])
        self.snapshot = None
        self.auto_fit = True
        self.answer_mode = False
        self._answer_press = None
        self.answer_marker = None
        self.detail_active = False
        self.cell_items = {}
        self.cells_by_code = {}
        for feature in geography["features"]:
            properties, geometry = feature["properties"], feature["geometry"]
            polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
            path = QPainterPath()
            path.setFillRule(Qt.FillRule.OddEvenFill)
            for polygon in polygons:
                for ring in polygon:
                    if not ring:
                        continue
                    path.moveTo(*project(*ring[0]))
                    previous = ring[0][0]
                    for point in ring[1:]:
                        # Не рисуем линию через весь мир при пересечении антимеридиана.
                        if abs(point[0] - previous) > 180:
                            path.moveTo(*project(*point))
                        else:
                            path.lineTo(*project(*point))
                        previous = point[0]
                    path.closeSubpath()
            item = QGraphicsPathItem(path)
            item.setData(0, properties["code"])
            item.setData(1, properties.get("country"))
            pen = QPen(QColor("#486071"), 0.55)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setBrush(QColor("#192a37"))
            item.setZValue(1 if properties.get("country") else 0)
            item.setVisible(not properties.get("country"))
            item.setToolTip(properties["label"])
            self.scene().addItem(item)
            self.items_by_code[properties["code"]] = item
            if not properties.get("country"):
                self.country_bounds[properties["code"]] = path.boundingRect()
        self.scene().setSceneRect(-183, -180, 366, 325)

    def fitInView(self, *args):
        super().fitInView(*args)
        self.viewport_changed.emit()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self.viewport_changed.emit()

    def showEvent(self, event):
        super().showEvent(event)
        self.viewport_changed.emit()

    def set_detail_active(self, enabled):
        if self.detail_active != enabled:
            self.detail_active = enabled
            if self.snapshot:
                self.update_snapshot(self.snapshot)

    def show_world(self):
        self.auto_fit = True
        self.fitInView(QRectF(-182, -155, 364, 275), Qt.AspectRatioMode.KeepAspectRatio)

    def focus_country(self, code):
        if code in self.country_bounds:
            self.auto_fit = False
            self.fitInView(self.country_bounds[code].adjusted(-2, -2, 2, 2), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.auto_fit:
            self.show_world()
        self.viewport_changed.emit()

    def wheelEvent(self, event):
        self.manual_navigation.emit()
        self.auto_fit = False
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        if 0.7 < self.transform().m11() * factor < 400000:
            self.scale(factor, factor)
            self.viewport_changed.emit()

    def mouseDoubleClickEvent(self, event):
        if self.answer_mode:
            event.accept()
            return
        self.manual_navigation.emit()
        if self.detail_active and self.transform().m11() > 200:
            point = self.mapToScene(event.position().toPoint())
            self.auto_fit = False
            self.scale(min(2, 399999 / self.transform().m11()), min(2, 399999 / self.transform().m11()))
            self.centerOn(point)
            self.viewport_changed.emit()
            return
        item = self.itemAt(event.position().toPoint())
        if item and item.data(2) in self.cells_by_code:
            self.focus_cell(item.data(2))
            return
        if item and (item.data(1) or item.data(0)):
            code = item.data(1) or item.data(0)
            self.country_clicked.emit(code)
            self.focus_country(code)
        else:
            if self.detail_active:
                point = self.mapToScene(event.position().toPoint())
                self.auto_fit = False
                self.scale(2, 2)
                self.centerOn(point)
                self.viewport_changed.emit()
            else:
                self.show_world()

    def mousePressEvent(self, event):
        if self.answer_mode and event.button() == Qt.MouseButton.LeftButton:
            self._answer_press = event.position().toPoint()
        if event.button() == Qt.MouseButton.LeftButton:
            self.manual_navigation.emit()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self.answer_mode and event.button() == Qt.MouseButton.LeftButton and self._answer_press is not None:
            start, self._answer_press = self._answer_press, None
            if (event.position().toPoint() - start).manhattanLength() <= 4:
                point = self.mapToScene(event.position().toPoint())
                if -180 <= point.x() <= 180 and -180 <= point.y() <= 180:
                    lon, lat = unproject(point.x(), point.y())
                    self.answer_clicked.emit(lat, lon)
        super().mouseReleaseEvent(event)

    def set_answer_mode(self, enabled):
        self.answer_mode = enabled
        self._answer_press = None
        self.viewport().setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.OpenHandCursor)
        if self.snapshot:
            self.update_snapshot(self.snapshot)

    @staticmethod
    def cell_rect(cell):
        west, south, east, north = cell.bounds
        left, top = project(west, north)
        right, bottom = project(east, south)
        return QRectF(left, top, right - left, bottom - top)

    def focus_cell(self, code):
        cell = self.cells_by_code.get(code)
        if cell:
            rect = self.cell_rect(cell)
            self.auto_fit = False
            self.fitInView(
                rect.adjusted(-rect.width(), -rect.height(), rect.width(), rect.height()),
                Qt.AspectRatioMode.KeepAspectRatio,
            )

    def show_answer(self, truth=None):
        if self.answer_marker is not None:
            self.scene().removeItem(self.answer_marker)
            self.answer_marker = None
        if truth is None:
            return
        pen = QPen(QColor("#ffffff"), 2)
        self.answer_marker = self.scene().addEllipse(-6, -6, 12, 12, pen, QBrush(QColor("#dc3ba4")))
        self.answer_marker.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.answer_marker.setPos(*project(truth.longitude, truth.latitude))
        self.answer_marker.setZValue(10)
        self.answer_marker.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.answer_marker.setToolTip("Правильный ответ")

    def focus_hypotheses(self, snapshot):
        scores = display_scores(snapshot, self.source)
        values = scores.countries
        boxes = self.country_bounds
        if self.level == "local" and scores.cells:
            values = {cell.code: cell.mass for cell in scores.cells}
            boxes = {cell.code: self.cell_rect(cell) for cell in scores.cells}
        if self.level == "regions" and scores.observed_regions:
            values = {
                code: value
                for code, value in scores.regions.items()
                if self.catalog.regions[code].country_code in scores.observed_regions
            }
            boxes = {code: self.items_by_code[code].boundingRect() for code in values}
            # Нельзя кадрировать по регионам, если ведущие страны ещё не исследованы.
            covered = sum(scores.countries[c] for c in scores.observed_regions)
            if covered < 0.55:
                values, boxes = scores.countries, self.country_bounds
        target = (
            focus_bounds(
                values,
                {
                    code: Bounds(rect.left(), rect.top(), rect.right(), rect.bottom())
                    for code, rect in boxes.items()
                },
                minimum=(0.7, 0.55) if self.level == "local" and scores.cells else (30, 22),
            )
            if scores.active
            else None
        )
        if target is None:
            self.show_world()
        else:
            self.auto_fit = False
            self.fitInView(
                QRectF(target.left, target.top, target.right - target.left, target.bottom - target.top),
                Qt.AspectRatioMode.KeepAspectRatio,
            )

    def set_level(self, level):
        self.level = level
        if self.snapshot:
            self.update_snapshot(self.snapshot)

    def update_snapshot(self, snapshot: RoundSnapshot):
        self.snapshot = snapshot
        scores = display_scores(snapshot, self.source)
        active = scores.active
        values = (
            scores.countries
            if self.level == "countries"
            else {cell.code: cell.mass for cell in scores.cells}
            if self.level == "local"
            else {
                code: value
                for code, value in scores.regions.items()
                if self.catalog.regions[code].country_code in scores.observed_regions
            }
        )
        scale_values = list(values.values()) if active else []
        if self.level == "local" and scale_values:
            scale_values.append(0)  # Остальная карта не имеет координатной поддержки.
        self.heat_scale = HeatScale.from_scores(scale_values, self.scale_mode)
        for code, item in self.items_by_code.items():
            country = item.data(1)
            if country:
                visible = self.level in ("regions", "local") and country in scores.observed_regions and active
                item.setVisible(visible)
                probability = scores.regions.get(code, 0)
                label = self.catalog.regions[code].label
            else:
                probability = scores.countries.get(code, 0)
                label = self.catalog.countries[code].label
            fill = self.heat_scale.intensity(probability) if active else 0
            if (not country and self.level != "countries") or self.level == "local":
                # Массу страны нельзя окрашивать по общей шкале совместных региональных оценок.
                fill = 0
            color = heat_color(fill)
            if self.detail_active:
                color.setAlphaF(0 if self.answer_mode or fill == 0 else 0.26)
            item.setBrush(QBrush(color))
            pen = item.pen()
            pen.setColor(QColor("#657d89") if self.detail_active else QColor("#486071"))
            pen.setWidthF(0.6 if self.detail_active else 0.55)
            item.setPen(pen)
            suffix = f"Оценка: {format_score(probability)}" if active else "Пока нет наблюдений"
            if country and active:
                country_score = scores.countries.get(country, 0)
                conditional = probability / country_score if country_score else 0
                suffix += f"\nВнутри страны: {format_score(conditional)}; страна: {format_score(country_score)}"
            if not country and self.level != "countries" and code not in scores.observed_regions:
                suffix += "\nРегион не анализировался"
            caption = "Раунд" if self.source == "round" else "Последний кадр"
            item.setToolTip(f"{label} · {caption}\n{suffix}\nОценки модели, не вероятность правильного ответа")
        for item in self.cell_items.values():
            self.scene().removeItem(item)
        self.cell_items = {}
        self.cells_by_code = {cell.code: cell for cell in scores.cells}
        if self.level == "local":
            for cell in scores.cells:
                path = QPainterPath()
                path.addRect(self.cell_rect(cell))
                parent = self.items_by_code.get(cell.region or cell.country)
                if parent:
                    path = path.intersected(parent.path())
                item = QGraphicsPathItem(path)
                color = heat_color(self.heat_scale.intensity(cell.mass))
                pen = QPen(color, 1.4)
                pen.setCosmetic(True)
                item.setPen(pen)
                color.setAlphaF(0 if self.answer_mode else 0.30 if self.detail_active else 0.80)
                item.setBrush(color)
                item.setZValue(3)
                item.setData(2, cell.code)
                lat, lon = cell.center
                place = self.catalog.regions.get(cell.region) or self.catalog.countries[cell.country]
                item.setToolTip(
                    f"{place.label} · зона ≈10 км\n{lat:.3f}, {lon:.3f}\n"
                    f"Поддержка: {format_score(cell.mass)}\nКоординаты совпадений Chipoint, не точность до адреса"
                )
                self.scene().addItem(item)
                self.cell_items[cell.code] = item
        self.scale_changed.emit(self.heat_scale)
