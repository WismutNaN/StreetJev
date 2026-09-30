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
    assert set(result) == {"roads", "labels"}
    assert result["roads"] == ((True, ((0, 0), (1, 1))),)
    assert result["labels"] == ((0.25, 0.125, "Инсбрук", 130000),)


def test_empty_tile_and_invalid_content():
    assert decode_details(b"") == {"roads": (), "labels": ()}
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
