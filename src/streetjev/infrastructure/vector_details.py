"""Спокойные ориентиры Shortbread: дороги, населённые пункты и вода."""

import math

from mapbox_vector_tile.decoder import TileData
from shapely.geometry import LineString, shape

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
WATER_KINDS = {"water", "river", "reservoir", "basin", "dock", "canal"}
LAYERS = {
    "streets",
    "place_labels",
    "water_polygons",
    "water_lines",
    "water_polygons_labels",
    "water_lines_labels",
    "street_labels",
}


def bilingual_name(props):
    local = str(props.get("name") or props.get("name_en") or "").strip()
    russian = str(props.get("name_ru") or "").strip()
    if russian and local and russian.casefold() != local.casefold():
        return f"{russian[:60]} / {local[:60]}"
    return (russian or local)[:100]


def lines_of(geometry):
    return [geometry["coordinates"]] if geometry["type"] == "LineString" else geometry["coordinates"]


def label_anchor(geometry):
    if geometry["type"] == "Point":
        return geometry["coordinates"]
    if geometry["type"] in ("LineString", "MultiLineString"):
        line = max(
            (LineString(p) for p in lines_of(geometry) if len(p) >= 2), key=lambda p: p.length, default=None
        )
        if line is not None:
            return line.interpolate(0.5, normalized=True).coords[0]
    return None


def landmarks_from_layer(decoder, layer, values, zoom):
    """Геометрия отбирается и упрощается в фоновом DecodeJob, до попадания в Qt."""
    polygons, rivers, labels = [], [], []
    extent = layer.extent
    candidates = []
    water = layer.name.startswith("water_")
    polygon = layer.name == "water_polygons"
    river = layer.name == "water_lines"
    min_water_area = (40075016.686 / 2**zoom / 512) ** 2 * 2
    for feature in layer.features:
        if len(feature.tags) % 2:
            raise ValueError("Некорректные свойства векторного тайла")
        p = {layer.keys[k]: values[v] for k, v in zip(feature.tags[::2], feature.tags[1::2])}
        kind = str(p.get("kind", "")).removesuffix("_link")
        if water:
            if kind not in WATER_KINDS or zoom < (9 if "lines" in layer.name else 4):
                continue
        elif zoom < max(9, ROAD_ZOOM.get(kind, 99)) or p.get("rail"):
            continue
        if polygon and feature.type != 3 or river and feature.type != 2:
            continue
        name = bilingual_name(p)
        if not water:
            ref = str(p.get("ref") or "")[:30]
            name = f"{ref} · {name}" if ref and name and ref != name else ref or name
        if not polygon and not river and not name:
            continue
        area = float(p.get("way_area") or 0)
        if polygon and 0 < area < min_water_area:
            continue
        priority = area if water and not river else len(feature.geometry) if river else -ROAD_ZOOM.get(kind, 99)
        candidates.append((priority, feature, name))
    # Порядок полигонов в MVT произвольный: лимит нельзя применять раньше сортировки,
    # иначе тысячи мелких прудов вытесняют крупное озеро в конце слоя.
    limit = 256 if polygon else 384 if river else 64
    for _, feature, name in sorted(candidates, key=lambda item: item[0], reverse=True)[:limit]:
        geometry = decoder.parse_geometry(feature.geometry, feature.type, extent, True, None)
        if polygon:
            simplified = shape(geometry).simplify(extent / 768, preserve_topology=True)
            parts = [simplified] if simplified.geom_type == "Polygon" else getattr(simplified, "geoms", [])
            for part in parts:
                if part.geom_type == "Polygon" and part.area >= extent * extent / 512**2:
                    polygons.append(
                        tuple(
                            tuple((x / extent, y / extent) for x, y in ring.coords)
                            for ring in [part.exterior, *part.interiors]
                        )
                    )
        elif river:
            for line in lines_of(geometry):
                if len(line) >= 2:
                    rivers.append(
                        tuple((x / extent, y / extent) for x, y in LineString(line).simplify(extent / 768).coords)
                    )
        else:
            anchor = label_anchor(geometry)
            if anchor and 0 <= anchor[0] < extent and 0 <= anchor[1] < extent:
                priority = 18000 if water else 8000
                labels.append(
                    (anchor[0] / extent, anchor[1] / extent, name, priority, "water" if water else "road")
                )
    return polygons, rivers, labels


def decode_details(content, zoom=14):
    # Читаем свойства до геометрии: здания, рельеф, POI и мелкие дороги даже не декодируются.
    # Низкоуровневый адаптер библиотеки изолирован здесь и проверяется MVT fixtures.
    decoder = TileData(content)
    roads, labels, water, rivers, landmarks = [], [], [], [], []
    for layer in decoder.tile.layers:
        if layer.name not in LAYERS:
            continue
        extent = layer.extent
        if extent <= 0:
            raise ValueError("Некорректный extent векторного тайла")
        values = [decoder.parse_value(value) for value in layer.values]
        if layer.name not in ("streets", "place_labels"):
            polygons, lines, names = landmarks_from_layer(decoder, layer, values, zoom)
            water.extend(polygons)
            rivers.extend(lines)
            landmarks.extend(names)
            continue
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
                name = bilingual_name(props)
                if not name:
                    continue
                geometry = decoder.parse_geometry(feature.geometry, 1, extent, True, None)
                if geometry["type"] != "Point":
                    continue
                x, y = geometry["coordinates"]
                if 0 <= x < extent and 0 <= y < extent:
                    priority = float(props.get("population") or 0)
                    labels.append((x / extent, y / extent, name, priority if math.isfinite(priority) else 0))
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
        "water": tuple(water),
        "rivers": tuple(rivers),
        "landmarks": tuple(landmarks),
    }
