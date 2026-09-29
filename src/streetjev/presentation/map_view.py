import math
from itertools import pairwise

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsPathItem, QGraphicsScene, QGraphicsView

from streetjev.domain.geography import GeoCatalog
from streetjev.domain.round import RoundSnapshot
from streetjev.presentation.map_scale import HeatScale, format_score
from streetjev.presentation.scores import display_scores


def project(lon, lat):
    lat = min(84, max(-84, lat))
    return lon, -math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


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
    country_clicked = Signal(str)
    scale_changed = Signal(object)

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

    def wheelEvent(self, event):
        self.auto_fit = False
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        if 0.7 < self.transform().m11() * factor < 700:
            self.scale(factor, factor)

    def mouseDoubleClickEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if item:
            code = item.data(1) or item.data(0)
            self.country_clicked.emit(code)
            self.focus_country(code)
        else:
            self.show_world()

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
            else {
                code: value
                for code, value in scores.regions.items()
                if self.catalog.regions[code].country_code in scores.observed_regions
            }
        )
        self.heat_scale = HeatScale.from_scores(values.values() if active else [], self.scale_mode)
        for code, item in self.items_by_code.items():
            country = item.data(1)
            if country:
                visible = self.level == "regions" and country in scores.observed_regions and active
                item.setVisible(visible)
                probability = scores.regions.get(code, 0)
                label = self.catalog.regions[code].label
            else:
                probability = scores.countries.get(code, 0)
                label = self.catalog.countries[code].label
            fill = self.heat_scale.intensity(probability) if active else 0
            if not country and self.level == "regions":
                # Массу страны нельзя окрашивать по общей шкале совместных региональных оценок.
                fill = 0
            item.setBrush(QBrush(heat_color(fill)))
            suffix = f"Оценка: {format_score(probability)}" if active else "Пока нет наблюдений"
            if country and active:
                country_score = scores.countries.get(country, 0)
                conditional = probability / country_score if country_score else 0
                suffix += f"\nВнутри страны: {format_score(conditional)}; страна: {format_score(country_score)}"
            if not country and self.level == "regions" and code not in scores.observed_regions:
                suffix += "\nРегион не анализировался"
            caption = "Раунд" if self.source == "round" else "Последний кадр"
            item.setToolTip(f"{label} · {caption}\n{suffix}\nОценки модели, не вероятность правильного ответа")
        self.scale_changed.emit(self.heat_scale)
