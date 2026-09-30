"""Геометрия карты и выбор области без пересчёта обводки."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainterPath
from PySide6.QtWidgets import QGraphicsPathItem

from streetjev.presentation.map_geometry import project


def geography_path(geometry):
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    for polygon in polygons:
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
    return path


class MapPolygon(QGraphicsPathItem):
    def __init__(self, path):
        super().__init__(path)
        self.original = path

    def shape(self):
        # Не строим дорогой stroked path ради выбора: косметическая обводка не расширяет страну.
        return self.original
