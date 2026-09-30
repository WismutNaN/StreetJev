"""Локальные совпадения: один кэшируемый прозрачный слой вместо сотен scene items."""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QRegion
from PySide6.QtWidgets import QGraphicsItem

from streetjev.presentation.map_geometry import cluster_points, radius_degrees, zoom_bucket
from streetjev.presentation.map_scale import HeatScale, support_color


class LocalOverlay(QGraphicsItem):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.points = ()
        self.clusters = ()
        self.markers = ()
        self.radius_km = 20
        self.mode = "relative"
        self.bucket = None
        self.image_key = None
        self.image = None
        self.circles = None
        self.circle_clip = None
        self.marker_colors = ()
        self.marker_intensities = ()
        self.composite_key = None
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
            self.markers = tuple(
                (
                    group,
                    radius_degrees(group.points[0].latitude, 1) if len(group.points) == 1 else 0,
                    support_color(self.scale.intensity(group.mass)),
                    self.scale.intensity(group.mass),
                )
                for group in self.clusters
            )
            self.bucket = bucket
            self.image_key = None

    def prepare_view(self):
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
        self.composite_key = None
        self.image = None
        self.visible_markers = []
        self.circle_clip = None
        if view.answer_mode or not self.clusters:
            self.circles = None
            return
        viewport = QRectF(0, 0, size.width(), size.height())
        colors, intensities = [], []
        sx, sy, dx, dy = transform.m11(), transform.m22(), transform.dx(), transform.dy()
        width, height = size.width(), size.height()
        for cluster, radius_unit, color, intensity in self.markers:
            radius = radius_unit * self.radius_km * sx
            extent = max(radius, 14)
            y = cluster.y * sy + dy
            if y + extent < 0 or y - extent > height:
                continue
            # При пересечении антимеридиана видна и вторая часть области поиска.
            for offset in (-360, 0, 360):
                x = (cluster.x + offset) * sx + dx
                if x + extent >= 0 and x - extent <= width:
                    self.visible_markers.append((QPointF(x, y), cluster, radius))
                    colors.append(color)
                    intensities.append(intensity)
        self.marker_colors, self.marker_intensities = colors, intensities
        # Круги — мягкий ориентир поиска. Ограничиваем размер их растра независимо от
        # DPI/zoom; точные маркеры рисуются далее в полном разрешении. Иначе сотни
        # перекрывающихся кругов заново заполняют миллиарды пикселей при каждом pan.
        circle_count = sum(radius >= 8 for _, _, radius in self.visible_markers)
        if circle_count == 0:
            self.circles = None
            return
        raster_limit = max(256, 640 / max(1, (circle_count / 128) ** 0.5))
        sharp_circles = 0 < circle_count <= 64 and max(r for _, _, r in self.visible_markers) <= 128
        raster_scale = min(ratio, (1024 if sharp_circles else raster_limit) / max(size.width(), size.height()))
        raster_size = QSize(
            max(1, round(size.width() * raster_scale)), max(1, round(size.height() * raster_scale))
        )
        halos = QImage(raster_size, QImage.Format.Format_ARGB32_Premultiplied)
        halos.fill(Qt.GlobalColor.transparent)
        brush = QPainter(halos)
        brush.scale(raster_scale, raster_scale)
        brush.setRenderHint(QPainter.RenderHint.Antialiasing, sharp_circles)
        brush.setPen(Qt.PenStyle.NoPen)

        def covers_view(center, radius):
            return radius**2 >= max(center.x() ** 2, (center.x() - width) ** 2) + max(
                center.y() ** 2, (center.y() - height) ** 2
            )

        # Последний круг, целиком покрывающий viewport, полностью скрывает заливки
        # слабее себя. Их можно пропустить без изменения итоговых цветов.
        first_halo = 0
        covers = []
        for i, (center, _, radius) in enumerate(self.visible_markers):
            covered = radius >= 8 and covers_view(center, radius)
            covers.append(covered)
            if covered:
                first_halo = i
        for i in range(first_halo, len(self.visible_markers)):
            center, _, radius = self.visible_markers[i]
            if radius >= 8:
                brush.setBrush(colors[i])
                if covers[i]:
                    brush.drawRect(viewport)
                else:
                    brush.drawEllipse(center, radius, radius)
        brush.end()
        circles = QImage(raster_size, QImage.Format.Format_ARGB32_Premultiplied)
        circles.fill(Qt.GlobalColor.transparent)
        brush = QPainter(circles)
        brush.setOpacity(0.15)
        brush.drawImage(QPointF(), halos)
        brush.setOpacity(1)
        brush.scale(raster_scale, raster_scale)
        brush.setRenderHint(QPainter.RenderHint.Antialiasing, sharp_circles)
        brush.setBrush(Qt.BrushStyle.NoBrush)
        for i, (center, _, radius) in enumerate(self.visible_markers):
            if radius >= 8 and not covers[i]:
                ring = QColor(colors[i])
                # Один пиксель малого растра, с поправкой яркости на его увеличение.
                # Финальное плавное увеличение сглаживает круг; точкам оно не нужно.
                ring.setAlphaF(0.8 if sharp_circles else 0.8 * min(1, raster_scale))
                brush.setPen(QPen(ring, 1 if sharp_circles else 0))
                brush.drawEllipse(center, radius, radius)
        brush.end()
        self.circles = circles
        if circle_count <= 64:
            clip = QRegion()
            padding = 3 / raster_scale
            for center, _, radius in self.visible_markers:
                if radius >= 8:
                    extent = radius + padding
                    bounds = QRectF(center.x() - extent, center.y() - extent, 2 * extent, 2 * extent)
                    clip |= QRegion(bounds.intersected(viewport).toAlignedRect())
            self.circle_clip = clip

    def draw(self, painter):
        viewport = QRectF(self.view.viewport().rect())
        if self.circles is not None:
            painter.save()
            if self.circle_clip is not None:
                painter.setClipRegion(self.circle_clip, Qt.ClipOperation.IntersectClip)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(viewport, self.circles)
            painter.restore()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont("Segoe UI")
        font.setPixelSize(11)
        painter.setFont(font)
        marker_bounds = viewport.adjusted(-14, -14, 14, 14)
        for i, (center, cluster, _) in enumerate(self.visible_markers):
            if not marker_bounds.contains(center):
                continue
            intensity = self.marker_intensities[i]
            count = len(cluster.points)
            dot_radius = 10 if count > 1 else 3.6 + 1.8 * intensity
            painter.setPen(QPen(QColor("#101e29"), 2))
            painter.setBrush(self.marker_colors[i])
            painter.drawEllipse(center, dot_radius, dot_radius)
            if count > 1:
                painter.setPen(QColor("#101e29" if intensity >= 0.3 else "#e0ebed"))
                painter.drawText(
                    QRectF(center.x() - 12, center.y() - 10, 24, 20),
                    Qt.AlignmentFlag.AlignCenter,
                    str(count) if count < 100 else "99+",
                )

    def _render(self):
        """Снимок прозрачного слоя для проверки пикселей; навигация не создаёт полноэкранную копию."""
        self.prepare_view()
        if not self.visible_markers or self.composite_key == self.image_key:
            return
        size, ratio = self.view.viewport().size(), self.view.devicePixelRatioF()
        self.image = QImage(
            max(1, int(size.width() * ratio)),
            max(1, int(size.height() * ratio)),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        self.image.setDevicePixelRatio(ratio)
        self.image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(self.image)
        self.draw(painter)
        painter.end()
        self.composite_key = self.image_key

    def hit(self, position):
        if self.view.answer_mode:
            return None
        self.prepare_view()
        candidates = [
            ((center - QPointF(position)).manhattanLength(), group) for center, group, _ in self.visible_markers
        ]
        distance, cluster = min(candidates, key=lambda pair: pair[0], default=(999, None))
        return cluster if distance <= 16 else None

    def paint(self, painter, option, widget=None):
        self.prepare_view()
        if self.visible_markers:
            painter.save()
            painter.resetTransform()
            self.draw(painter)
            painter.restore()
