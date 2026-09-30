"""Локальные совпадения: один кэшируемый прозрачный слой вместо сотен scene items."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem

from streetjev.presentation.map_geometry import cluster_points, radius_degrees, zoom_bucket
from streetjev.presentation.map_scale import HeatScale, support_color


class LocalOverlay(QGraphicsItem):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.points = ()
        self.clusters = ()
        self.radius_km = 20
        self.mode = "relative"
        self.bucket = None
        self.image_key = None
        self.image = None
        self.visible_markers = []
        self.scale = HeatScale.from_scores([])
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setZValue(5)

    def boundingRect(self):
        return QRectF(-180, -180, 360, 360)

    def shape(self):
        return QPainterPath()  # Подсказки/клики обрабатывает MapView, остальная карта доступна.

    def set_points(self, points, mode):
        if self.points != points or self.mode != mode:
            self.points, self.mode = tuple(points), mode
            self.bucket = self.image_key = None
            self.update()

    def set_radius(self, radius):
        if self.radius_km != radius:
            self.radius_km = radius
            self.image_key = None
            self.update()

    def prepare(self):
        bucket = zoom_bucket(self.view.transform().m11())
        if bucket != self.bucket:
            self.clusters = cluster_points(self.points, bucket)
            self.scale = HeatScale.from_scores(
                [0, *(p.mass for p in self.clusters)] if self.clusters else [], self.mode
            )
            self.bucket = bucket
            self.image_key = None

    def _render(self):
        self.prepare()
        view = self.view
        transform = view.viewportTransform()
        size, ratio = view.viewport().size(), view.devicePixelRatioF()
        key = (
            transform.m11(),
            transform.m22(),
            transform.dx(),
            transform.dy(),
            size,
            ratio,
            self.radius_km,
            view.answer_mode,
        )
        if key == self.image_key:
            return
        self.image_key = key
        self.visible_markers = []
        if view.answer_mode or not self.clusters:
            self.image = None
            return
        image = QImage(
            max(1, int(size.width() * ratio)),
            max(1, int(size.height() * ratio)),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        self.image = image
        viewport = QRectF(0, 0, size.width(), size.height())
        for cluster in self.clusters:
            radius = (
                radius_degrees(cluster.points[0].latitude, self.radius_km) * transform.m11()
                if len(cluster.points) == 1
                else 0
            )
            # При пересечении антимеридиана видна и вторая часть области поиска.
            for offset in (-360, 0, 360):
                center = transform.map(QPointF(cluster.x + offset, cluster.y))
                extent = max(radius, 14)
                if viewport.intersects(QRectF(center.x() - extent, center.y() - extent, 2 * extent, 2 * extent)):
                    self.visible_markers.append((center, cluster, radius))
        # Непрозрачные фигуры сначала компонуются отдельно, затем весь слой получает alpha.
        # Пересечение кругов не суммирует непрозрачность и не изображает рост уверенности.
        halos = QImage(image.size(), image.format())
        halos.setDevicePixelRatio(ratio)
        halos.fill(Qt.GlobalColor.transparent)
        brush = QPainter(halos)
        brush.setRenderHint(QPainter.RenderHint.Antialiasing)
        brush.setPen(Qt.PenStyle.NoPen)

        def covers_view(center, radius):
            return radius**2 >= max(
                (center.x() - x) ** 2 + (center.y() - y) ** 2
                for x in (0, size.width())
                for y in (0, size.height())
            )

        for center, cluster, radius in self.visible_markers:
            if radius >= 8:
                brush.setBrush(support_color(self.scale.intensity(cluster.mass)))
                if covers_view(center, radius):
                    brush.drawRect(viewport)
                else:
                    brush.drawEllipse(center, radius, radius)
        brush.end()
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(0.15)
        painter.drawImage(QPointF(), halos)
        painter.setOpacity(1)
        font = QFont("Segoe UI")
        font.setPixelSize(11)
        painter.setFont(font)
        for center, cluster, radius in self.visible_markers:
            intensity = self.scale.intensity(cluster.mass)
            color = support_color(intensity)
            if radius >= 8 and not covers_view(center, radius):
                ring = QColor(color)
                ring.setAlphaF(0.8)
                painter.setPen(QPen(ring, 1.1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(center, radius, radius)
            count = len(cluster.points)
            dot_radius = 10 if count > 1 else 3.6 + 1.8 * intensity
            painter.setPen(QPen(QColor("#101e29"), 2))
            painter.setBrush(color)
            painter.drawEllipse(center, dot_radius, dot_radius)
            if count > 1:
                painter.setPen(QColor("#101e29" if intensity >= 0.3 else "#e0ebed"))
                painter.drawText(
                    QRectF(center.x() - 12, center.y() - 10, 24, 20),
                    Qt.AlignmentFlag.AlignCenter,
                    str(count) if count < 100 else "99+",
                )
        painter.end()

    def hit(self, position):
        if self.view.answer_mode:
            return None
        self._render()
        candidates = [
            ((center - QPointF(position)).manhattanLength(), group) for center, group, _ in self.visible_markers
        ]
        distance, cluster = min(candidates, key=lambda pair: pair[0], default=(999, None))
        return cluster if distance <= 16 else None

    def paint(self, painter, option, widget=None):
        self._render()
        if self.image is not None:
            painter.save()
            painter.resetTransform()
            painter.drawImage(QPointF(), self.image)
            painter.restore()
