"""Из Shortbread MVT берём только дорожные линии и подписи населённых пунктов."""

import math

from mapbox_vector_tile.decoder import TileData
from shapely.geometry import LineString

ROAD_ZOOM = {
    "motorway": 5,
    "trunk": 5,
    "primary": 7,
    "secondary": 9,
    "tertiary": 11,
    "unclassified": 12,
    "residential": 14,
}
PLACE_ZOOM = {"capital": 0, "state_capital": 4, "city": 5, "town": 8, "village": 11, "hamlet": 13}
MAX_ROADS = 2400
MAX_LABELS = 48


def decode_details(content, zoom=14):
    # Читаем свойства до геометрии: здания, рельеф, POI и мелкие дороги даже не декодируются.
    # Низкоуровневый адаптер библиотеки изолирован здесь и проверяется MVT fixtures.
    decoder = TileData(content)
    roads, labels = [], []
    for layer in decoder.tile.layers:
        if layer.name not in ("streets", "place_labels"):
            continue
        extent = layer.extent
        if extent <= 0:
            raise ValueError("Некорректный extent векторного тайла")
        values = [decoder.parse_value(value) for value in layer.values]
        candidates = []
        for feature in layer.features:
            if len(feature.tags) % 2:
                raise ValueError("Некорректные свойства векторного тайла")
            props = {layer.keys[k]: values[v] for k, v in zip(feature.tags[::2], feature.tags[1::2])}
            kind = str(props.get("kind", "")).removesuffix("_link")
            if layer.name == "streets":
                minimum = ROAD_ZOOM.get(kind, 99)
                if feature.type == 2 and zoom >= minimum and not props.get("rail"):
                    candidates.append((minimum, feature, kind))
            elif feature.type == 1 and zoom >= PLACE_ZOOM.get(kind, 99):
                name = props.get("name_ru") or props.get("name") or props.get("name_en")
                if not name:
                    continue
                geometry = decoder.parse_geometry(feature.geometry, 1, extent, True, None)
                if geometry["type"] != "Point":
                    continue
                x, y = geometry["coordinates"]
                if 0 <= x < extent and 0 <= y < extent:
                    priority = float(props.get("population") or 0)
                    labels.append(
                        (x / extent, y / extent, str(name)[:100], priority if math.isfinite(priority) else 0)
                    )
        for _, feature, kind in sorted(candidates, key=lambda item: item[0])[:MAX_ROADS]:
            geometry = decoder.parse_geometry(feature.geometry, 2, extent, True, None)
            lines = [geometry["coordinates"]] if geometry["type"] == "LineString" else geometry["coordinates"]
            for line in lines:
                if len(line) < 2:
                    continue
                # Отклонение меньше пикселя при штатном размере тайла, без сдвига концов дорог.
                simplified = LineString(line).simplify(extent / 768, preserve_topology=False)
                points = tuple((x / extent, y / extent) for x, y in simplified.coords)
                if len(points) > 1:
                    roads.append((kind in {"motorway", "trunk", "primary", "secondary"}, points))
    return {
        "roads": tuple(roads),
        "labels": tuple(sorted(labels, key=lambda item: item[3], reverse=True)[:MAX_LABELS]),
    }
