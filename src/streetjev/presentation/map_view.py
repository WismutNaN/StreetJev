from PySide6.QtCore import QEvent, QLineF, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QStaticText
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsView, QToolTip

from streetjev.domain.geography import GeoCatalog
from streetjev.domain.round import RoundSnapshot
from streetjev.presentation.auto_focus import Bounds, focus_bounds
from streetjev.presentation.local_overlay import LocalOverlay
from streetjev.presentation.map_geometry import project, radius_degrees, unproject
from streetjev.presentation.map_scale import HeatScale, format_score, support_color
from streetjev.presentation.scores import display_scores


class CountryOutline(QGraphicsPathItem):
    def __init__(self, path):
        super().__init__(path)
        self.lines = [
            QLineF(a, b) for polygon in path.toSubpathPolygons() for a, b in zip(polygon, list(polygon)[1:])
        ]

        self.color, self.width = QColor(), 1
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setZValue(2)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def shape(self):
        return QPainterPath()

    def paint(self, painter, option, widget=None):
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Кэшируем сегменты: Qt строит дорогую геометрию соединений для широкого drawPath.
        # drawLines не пересобирает её и использует только прямоугольное отсечение viewport.
        pen = QPen(self.color, self.width / 2)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        painter.drawLines(self.lines)


class MapView(QGraphicsView):
    viewport_changed = Signal()
    answer_clicked = Signal(float, float)
    scale_changed = Signal(object)
    manual_navigation = Signal()
    location_selected = Signal(str)

    def __init__(self, catalog: GeoCatalog, geography: dict):
        super().__init__()
        self.catalog = catalog
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#101e29"))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.items_by_code = {}
        self.country_bounds = {}
        self.source = "round"
        self.scale_mode = "relative"
        self.heat_scale = HeatScale.from_scores([])
        self.country_scale = HeatScale.from_scores([])
        self.snapshot = None
        self.auto_fit = True
        self.left_inset = 0
        self.answer_mode = False
        self._answer_press = None
        self._map_press = None
        self.answer_marker = None
        self.detail_active = False
        self.detail_labels = []
        self.country_labels = []
        self._label_key = None
        self._label_layout = []
        self._label_texts = {}
        self._label_font = QFont("Segoe UI")
        self._label_font.setPixelSize(11)
        self.country_outlines = {}
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
            item.setBrush(QColor("#1a2d3b"))
            item.setZValue(1 if properties.get("country") else 0)
            item.setVisible(not properties.get("country"))
            item.setToolTip(properties["label"])
            self.scene().addItem(item)
            self.items_by_code[properties["code"]] = item
            if not properties.get("country"):
                self.country_bounds[properties["code"]] = path.boundingRect()
                outline = CountryOutline(path)
                outline.hide()
                self.scene().addItem(outline)
                self.country_outlines[properties["code"]] = outline
        self.local_overlay = LocalOverlay(self)
        self.scene().addItem(self.local_overlay)
        self.scene().setSceneRect(-183, -180, 366, 325)

    def fitInView(self, *args):
        if args and isinstance(args[0], QRectF) and self.left_inset:
            rectangle = QRectF(args[0])
            width, height = self.viewport().width() - 8, self.viewport().height() - 8
            inset = min(self.left_inset, max(0, width - 100))
            scale = min((width - inset) / max(rectangle.width(), 1e-9), height / max(rectangle.height(), 1e-9))
            if scale > 0:
                rectangle.adjust(-inset / scale, 0, 0, 0)
            args = (rectangle, *args[1:])
        super().fitInView(*args)
        if self.transform().m11() > 399999:
            center = self.mapToScene(self.viewport().rect().center())
            factor = 399999 / self.transform().m11()
            self.scale(factor, factor)
            self.centerOn(center)
        self.viewport_changed.emit()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self.viewport_changed.emit()

    def showEvent(self, event):
        super().showEvent(event)
        self.viewport_changed.emit()

    def set_detail_active(self, enabled):
        self.detail_active = enabled

    def set_detail_labels(self, labels):
        self.detail_labels = labels
        self._label_key = None
        names = {item[2] for item in [*labels, *self.country_labels]}
        self._label_texts = {name: text for name, text in self._label_texts.items() if name in names}
        self.viewport().update()

    def detail_label_layout(self):
        transform = self.viewportTransform()
        marker = self.mapFromScene(self.answer_marker.pos()) if self.answer_marker else None
        key = (transform.m11(), transform.m22(), transform.dx(), transform.dy(), self.viewport().size(), marker)
        if key == self._label_key:
            return self._label_layout
        occupied, layout = [], []
        viewport = QRectF(self.viewport().rect())
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        limit = max(6, min(32, self.viewport().width() * self.viewport().height() // 22000))
        for x, y, name, _population in [*self.country_labels, *self.detail_labels]:
            if not visible.contains(QPointF(x, y)):
                continue
            text = self._label_texts.get(name)
            if text is None:
                text = QStaticText(name)
                text.setTextFormat(Qt.TextFormat.PlainText)
                text.prepare(font=self._label_font)
                self._label_texts[name] = text
            point = QPointF(self.mapFromScene(QPointF(x, y)))
            origin = point + QPointF(4, -text.size().height() - 2)
            box = QRectF(origin, text.size()).adjusted(-18, -9, 18, 9)
            if (
                not viewport.contains(box)
                or any(box.intersects(previous) for previous in occupied)
                or marker is not None
                and box.intersects(QRectF(marker.x() - 8, marker.y() - 8, 16, 16))
            ):
                continue
            occupied.append(box)
            layout.append((point, origin, text))
            if len(layout) >= limit:
                break
        self._label_key, self._label_layout = key, layout
        return layout

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if not self.detail_labels and not self.country_labels:
            return
        painter.save()
        painter.resetTransform()
        painter.setFont(self._label_font)
        for point, origin, text in self.detail_label_layout():
            painter.setPen(QColor(12, 22, 29, 180))
            painter.drawStaticText(origin + QPointF(0, 1), text)
            painter.setPen(QColor(165, 179, 188, 210))
            painter.drawStaticText(origin, text)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(150, 164, 173, 170))
            painter.drawEllipse(point, 1.2, 1.2)
        painter.restore()

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
        cluster = self.local_overlay.hit(event.position().toPoint())
        if cluster:
            self.focus_cluster(cluster)
            return
        if self.detail_active and self.transform().m11() > 200:
            point = self.mapToScene(event.position().toPoint())
            self.auto_fit = False
            self.scale(min(2, 399999 / self.transform().m11()), min(2, 399999 / self.transform().m11()))
            self.centerOn(point)
            self.viewport_changed.emit()
            return
        item = self.itemAt(event.position().toPoint())
        if item and (item.data(1) or item.data(0)):
            self.focus_place(item.data(0))
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
            self._map_press = event.position().toPoint()
            self.manual_navigation.emit()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        point = event.position().toPoint()
        if (
            not self.answer_mode
            and self._map_press is not None
            and (point - self._map_press).manhattanLength() <= 4
        ):
            message = self.location_description(point)
            if message:
                self.location_selected.emit(message.replace("\n", " · "))
        self._map_press = None
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

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.ToolTip and hasattr(self, "local_overlay"):
            cluster = self.local_overlay.hit(event.pos())
            if cluster:
                QToolTip.showText(event.globalPos(), self.cluster_description(cluster), self.viewport())
                return True
        return super().viewportEvent(event)

    def location_description(self, position):
        cluster = self.local_overlay.hit(position)
        if cluster:
            return self.cluster_description(cluster)
        item = self.itemAt(position)
        return item.toolTip() if item else ""

    def cluster_description(self, cluster):
        scores = display_scores(self.snapshot, self.source)
        first = cluster.points[0]
        lines = [f"{self.catalog.countries[first.country].label}: {format_score(scores.countries[first.country])}"]
        if first.region:
            lines.append(
                f"{self.catalog.regions[first.region].label}: {format_score(scores.regions.get(first.region, 0))}"
            )
        if len(cluster.points) == 1:
            lines.append(f"Chipoint: {first.latitude:.5f}, {first.longitude:.5f} · {format_score(cluster.mass)}")
            lines.append(f"Радиус поиска ≈{self.local_overlay.radius_km} км; не точность модели")
        else:
            lines.append(f"Chipoint: {len(cluster.points)} совпадений · {format_score(cluster.mass)}")
            lines.append("Группа для текущего масштаба; двойной клик раскрывает точки")
        return "\n".join(lines)

    def focus_place(self, code):
        if code in self.country_bounds:
            self.focus_country(code)
        elif code in self.items_by_code:
            self.auto_fit = False
            rect = self.items_by_code[code].boundingRect()
            self.fitInView(rect.adjusted(-0.4, -0.4, 0.4, 0.4), Qt.AspectRatioMode.KeepAspectRatio)

    def focus_point(self, point):
        x, y = project(point.longitude, point.latitude)
        radius = radius_degrees(point.latitude, self.local_overlay.radius_km) * 1.6
        self.auto_fit = False
        self.fitInView(QRectF(x - radius, y - radius, 2 * radius, 2 * radius), Qt.AspectRatioMode.KeepAspectRatio)

    def focus_cluster(self, cluster):
        if len(cluster.points) == 1:
            self.focus_point(cluster.points[0])
        else:
            positions = [project(p.longitude, p.latitude) for p in cluster.points]
            left, right = min(p[0] for p in positions), max(p[0] for p in positions)
            top, bottom = min(p[1] for p in positions), max(p[1] for p in positions)
            margin = max(0.000001, max(right - left, bottom - top) * 0.25)
            self.auto_fit = False
            self.fitInView(
                QRectF(left - margin, top - margin, right - left + 2 * margin, bottom - top + 2 * margin),
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
        values, boxes = {}, {}
        # Используем совместные оценки: регион заменяет свою страну, а не добавляет её массу второй раз.
        for country, mass in scores.countries.items():
            if country in scores.observed_regions:
                for region in self.catalog.regions_of(country):
                    values[region.code] = scores.regions.get(region.code, 0)
                    boxes[region.code] = self.items_by_code[region.code].boundingRect()
            else:
                values[country] = mass
                boxes[country] = self.country_bounds[country]
        covered = sum(scores.countries[c] for c in scores.observed_regions)
        target = (
            focus_bounds(
                values,
                {
                    code: Bounds(rect.left(), rect.top(), rect.right(), rect.bottom())
                    for code, rect in boxes.items()
                },
                minimum=(8, 6) if covered >= 0.55 else (30, 22),
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

    def update_snapshot(self, snapshot: RoundSnapshot):
        self.snapshot = snapshot
        scores = display_scores(snapshot, self.source)
        active = scores.active
        values = {
            code: value
            for code, value in scores.regions.items()
            if self.catalog.regions[code].country_code in scores.observed_regions
        }
        self.country_scale = HeatScale.from_scores(scores.countries.values() if active else [], self.scale_mode)
        self.heat_scale = HeatScale.from_scores(values.values() if active else [], self.scale_mode)
        for code, item in self.items_by_code.items():
            country = item.data(1)
            if country:
                item.setVisible(active and country in scores.observed_regions)
                probability = values.get(code, 0)
                color = support_color(self.heat_scale.intensity(probability))
                color.setAlphaF(0.42 if active and probability > 0 and not self.answer_mode else 0)
                item.setBrush(color)
                label = self.catalog.regions[code].label
                item.setToolTip(
                    f"{self.catalog.countries[country].label}: {format_score(scores.countries.get(country, 0))}\n"
                    f"{label}: {format_score(probability)}\nСовместная поддержка региона, не вероятность правильного ответа"
                )
            else:
                probability = scores.countries.get(code, 0)
                item.setBrush(QColor("#1a2d3b"))
                label = self.catalog.countries[code].label
                item.setToolTip(f"{label}: {format_score(probability)}" if active else label)
                if active and code not in scores.observed_regions:
                    item.setToolTip(item.toolTip() + "\nРегионы не анализировались")
                outline = self.country_outlines[code]
                intensity = self.country_scale.intensity(probability)
                outline.setVisible(
                    active and not self.answer_mode and probability > 0 and not self.country_scale.flat
                )
                outline.color = support_color(intensity)
                outline.color.setAlphaF(0.25 + 0.6 * intensity)
                outline.width = 2 * (0.55 + 1.8 * intensity**0.5)
                outline.setZValue(2 + intensity)
                outline.update()
            pen = QPen(QColor("#365061"), 0.45 if country else 0.55)
            pen.setCosmetic(True)
            item.setPen(pen)
        self.country_labels = []
        if active and not self.answer_mode and not self.country_scale.flat:
            leaders = sorted(scores.countries, key=scores.countries.get, reverse=True)[:5]
            for code in leaders:
                if scores.countries[code] < scores.countries[leaders[0]] * 0.1:
                    continue
                center = self.country_bounds[code].center()
                self.country_labels.append(
                    (
                        center.x(),
                        center.y(),
                        f"{self.catalog.countries[code].label} · {format_score(scores.countries[code])}",
                        1e12,
                    )
                )
        self._label_key = None
        names = {item[2] for item in [*self.detail_labels, *self.country_labels]}
        self._label_texts = {name: text for name, text in self._label_texts.items() if name in names}
        self.local_overlay.set_points(scores.points if active else (), self.scale_mode)
        self.local_overlay.image_key = None
        self.local_overlay.update()
        self.scale_changed.emit(self.heat_scale)
        self.viewport().update()
