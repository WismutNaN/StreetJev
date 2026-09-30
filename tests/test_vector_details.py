import mapbox_vector_tile
import pytest
from google.protobuf.message import DecodeError

from streetjev.infrastructure.vector_details import decode_details


def test_only_roads_and_places_y_points_down_and_russian_names():
    content = mapbox_vector_tile.encode(
        [
            {
                "name": "streets",
                "features": [
                    {"geometry": "LINESTRING(0 0, 4096 4096)", "properties": {"kind": "primary"}},
                    {"geometry": "LINESTRING(0 1, 20 30)", "properties": {"kind": "rail", "rail": True}},
                    {"geometry": "POLYGON((0 0,1 0,1 1,0 1,0 0))", "properties": {"kind": "pedestrian"}},
                ],
            },
            {
                "name": "place_labels",
                "features": [
                    {
                        "geometry": "POINT(1024 512)",
                        "properties": {
                            "kind": "city",
                            "name": "Innsbruck",
                            "name_ru": "Инсбрук",
                            "population": 130000,
                        },
                    },
                    {"geometry": "POINT(4100 512)", "properties": {"kind": "town", "name": "Outside"}},
                ],
            },
            {
                "name": "water_polygons",
                "features": [{"geometry": "POLYGON((0 0,4096 0,4096 4096,0 4096,0 0))", "properties": {}}],
            },
            {
                "name": "buildings",
                "features": [{"geometry": "POLYGON((0 0,4096 0,4096 4096,0 4096,0 0))", "properties": {}}],
            },
        ],
        default_options={"y_coord_down": True},
    )
    result = decode_details(content)
    assert set(result) == {"roads", "labels", "water", "rivers", "landmarks"}
    assert result["roads"] == ((True, ((0, 0), (1, 1))),)
    assert result["labels"] == ((0.25, 0.125, "Инсбрук / Innsbruck", 130000),)
    assert not result["water"]  # Неизвестный kind не превращается в воду.


def test_empty_tile_and_invalid_content():
    assert decode_details(b"") == {"roads": (), "labels": (), "water": (), "rivers": (), "landmarks": ()}
    with pytest.raises(DecodeError):
        decode_details(b"not a vector tile")


def test_zoom_filters_clutter_before_geometry_decode_and_simplifies():
    kinds = ["motorway", "primary", "secondary", "tertiary", "residential", "service", "footway", "track"]
    content = mapbox_vector_tile.encode(
        [
            {
                "name": "streets",
                "features": [
                    {"geometry": "LINESTRING(0 0, 512 1, 1024 0, 2048 1, 4096 0)", "properties": {"kind": kind}}
                    for kind in kinds
                ],
            },
            {
                "name": "place_labels",
                "features": [
                    {"geometry": "POINT(100 100)", "properties": {"kind": kind, "name": kind, "population": 1}}
                    for kind in ["city", "town", "village", "hamlet", "neighbourhood"]
                ],
            },
        ],
        default_options={"y_coord_down": True},
    )
    assert len(decode_details(content, 6)["roads"]) == 1
    assert len(decode_details(content, 10)["roads"]) == 3
    result = decode_details(content, 14)
    assert len(result["roads"]) == 5
    assert all(len(points) == 2 and points == ((0, 0), (1, 0)) for _, points in result["roads"])
    assert [label[2] for label in decode_details(content, 6)["labels"]] == ["city"]
    assert len(result["labels"]) == 4


def test_water_holes_rivers_road_numbers_bilingual_and_excluded_terrain():
    polygon = "POLYGON((0 0,4096 0,4096 4096,0 4096,0 0),(1024 1024,1024 2048,2048 2048,2048 1024,1024 1024))"
    content = mapbox_vector_tile.encode(
        [
            {
                "name": "water_polygons",
                "features": [
                    {"geometry": polygon, "properties": {"kind": "water"}},
                    {"geometry": polygon, "properties": {"kind": "glacier"}},
                ],
            },
            {
                "name": "water_lines",
                "features": [
                    {"geometry": "LINESTRING(0 0,4096 4096)", "properties": {"kind": "river"}},
                    {"geometry": "LINESTRING(0 0,4096 4096)", "properties": {"kind": "ditch"}},
                ],
            },
            {
                "name": "water_polygons_labels",
                "features": [
                    {
                        "geometry": "POINT(500 500)",
                        "properties": {"kind": "water", "name": "Lake", "name_ru": "Озеро"},
                    },
                ],
            },
            {
                "name": "street_labels",
                "features": [
                    {
                        "geometry": "LINESTRING(0 0,4096 4096)",
                        "properties": {"kind": "primary", "ref": "A12", "name": "Main Road"},
                    },
                    {"geometry": "LINESTRING(0 0,4096 4096)", "properties": {"kind": "rail", "ref": "123"}},
                ],
            },
        ],
        default_options={"y_coord_down": True},
    )
    result = decode_details(content, 12)
    assert len(result["water"]) == 1
    assert len(result["water"][0]) == 2  # Остров не закрашивается водой.
    assert len(result["rivers"]) == 1
    assert {label[2] for label in result["landmarks"]} == {"Озеро / Lake", "A12 · Main Road"}
    road = next(label for label in result["landmarks"] if label[-1] == "road")
    assert road[:2] == (0.5, 0.5)
    assert not decode_details(content, 3)["water"]


def test_large_lake_at_end_of_layer_is_not_displaced_by_small_ponds():
    small = {"geometry": "POLYGON((1 1,2 1,2 2,1 2,1 1))", "properties": {"kind": "water", "way_area": 10}}
    lake = {
        "geometry": "POLYGON((0 0,4096 0,4096 4096,0 4096,0 0))",
        "properties": {"kind": "water", "way_area": 50000000},
    }
    tile = mapbox_vector_tile.encode(
        {"name": "water_polygons", "features": [small] * 1000 + [lake]}, default_options={"y_coord_down": True}
    )
    result = decode_details(tile, 10)
    assert len(result["water"]) == 1
    assert max(x for x, y in result["water"][0][0]) == 1
