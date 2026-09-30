"""Кэш подложки, точный hit-test, отсечение контуров и завершение drag/wheel без сети."""

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QTransform, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleOptionGraphicsItem

from streetjev.domain.geography import GeoCatalog, Place
from streetjev.presentation.map_polygon import MapPolygon
from streetjev.presentation.map_view import CountryOutline, MapView
from streetjev.presentation.tile_layer import RoadTile, TileRasterCache


def main():
    app = QApplication([])
    shape = QPainterPath()
    shape.addRect(0, 0, 10, 10)
    shape.addRect(4, 4, 2, 2)
    polygon = MapPolygon(shape)
    pen = QPen(QColor("#365061"), 0.55)
    pen.setCosmetic(True)
    polygon.setPen(pen)
    assert polygon.contains(QPointF(0.5, 0.5)), "Hit-test использует исходную границу"
    assert not polygon.contains(QPointF(5, 5)), "Отверстие не должно стать сушей"
    assert not polygon.contains(QPointF(-0.1, 5)), "Cosmetic pen не расширяет область выбора"

    # В кадре должна остаться та же граница, включая сегменты, пересекающие край.
    for i in range(200):
        shape.addRect(100 + i, 100, 0.3, 0.3)
    outline = CountryOutline(shape)
    outline.color, outline.width = QColor("#65dab6"), 3
    option = QStyleOptionGraphicsItem()
    option.exposedRect = QRectF(-1, -1, 12, 12)
    rendered = []
    for culled in (False, True):
        image = QImage(240, 240, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(20, 20)
        painter.translate(1, 1)
        if culled:
            outline.paint(painter, option)
        else:
            pen = QPen(outline.color, outline.width / 2)
            pen.setCosmetic(True)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            painter.drawLines(outline.lines)
        painter.end()
        rendered.append(image)
    assert rendered[0] == rendered[1]

    # Озеро с островом, линия дороги и пустой участок. Повторный pan/zoom не рисует вектор заново.
    details = {
        "roads": [(True, [(0.1, 0.1), (0.9, 0.1)])],
        "labels": [],
        "water": [
            [
                [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8), (0.2, 0.2)],
                [(0.4, 0.4), (0.6, 0.4), (0.6, 0.6), (0.4, 0.6), (0.4, 0.4)],
            ]
        ],
    }
    cache = TileRasterCache()
    tile = RoadTile((1, 1, 1), details, cache)
    with patch.object(tile, "draw_vectors", wraps=tile.draw_vectors) as draw:
        for factor, offset in ((1, 0), (1, 10), (1.2, 15)):
            image = QImage(512, 512, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.translate(offset, offset)
            painter.scale(factor * 512 / 180, factor * 512 / 180)
            tile.paint(painter, option)
            painter.end()
            if factor == 1 and offset == 0:
                assert image.pixelColor(150, 150) == QColor("#243f50")
                assert image.pixelColor(256, 256).alpha() == image.pixelColor(10, 10).alpha() == 0
        assert draw.call_count == 1
        painter = QPainter(image)
        painter.scale(2048 / 180, 2048 / 180)
        tile.paint(painter, option)
        painter.end()
        assert draw.call_count == 2, "На предельном zoom дороги сохраняют косметическую толщину"
    cache.reserve(96)
    assert 96 * cache.maximum_side**2 * 4 <= cache.limit
    tiny = TileRasterCache(2 * 32 * 32 * 4)
    sample = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
    tiny.put("a", sample)
    tiny.put("b", sample)
    tiny.get("a")
    tiny.put("c", sample)
    assert tiny.get("b") is None and tiny.get("a") is not None and tiny.size <= tiny.limit

    catalog = GeoCatalog((Place("AT", "Austria", "Австрия"),), ())
    geography = {
        "features": [
            {
                "properties": {"code": "AT", "label": "Австрия"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[(10, 45), (20, 45), (20, 50), (10, 50), (10, 45)]],
                },
            }
        ]
    }
    view = MapView(catalog, geography)
    view.resize(800, 600)
    view.auto_fit = False
    view.setTransform(QTransform.fromScale(60, 60))
    view.show()
    app.processEvents()
    center = view.viewport().rect().center()
    before = view.viewportTransform()
    view.set_answer_mode(True)
    answers = []
    view.answer_clicked.connect(lambda lat, lon: answers.append((lat, lon)))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=center)
    QTest.mouseMove(view.viewport(), center + QPoint(30, 0))
    app.processEvents()
    assert view.viewportTransform() != before and view.interacting
    assert not view.renderHints() & QPainter.RenderHint.Antialiasing
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=center + QPoint(30, 0))
    assert not answers
    QTest.qWait(250)
    assert not view.interacting and view.renderHints() & QPainter.RenderHint.Antialiasing
    wheel = QWheelEvent(
        QPointF(center),
        QPointF(view.viewport().mapToGlobal(center)),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    app.sendEvent(view.viewport(), wheel)
    assert view.interacting
    QTest.qWait(250)
    assert not view.interacting and view.renderHints() & QPainter.RenderHint.Antialiasing
    view.close()
    print("Map rendering smoke — OK: bounded raster reuse, holes, exact selection, outline clipping, drag/wheel")


if __name__ == "__main__":
    main()
