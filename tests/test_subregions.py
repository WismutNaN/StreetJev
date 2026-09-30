import gzip
import json
from dataclasses import replace

import pytest
from shapely.geometry import box, mapping

from streetjev.domain.localization import LocalPoint
from streetjev.domain.subregions import Subregion, subregion_support
from streetjev.infrastructure.subregions import SubregionDataset, SubregionRepository


def fixture():
    geography = {
        "features": [
            {"properties": {"code": code, "country": "AT"}, "geometry": mapping(geometry)}
            for code, geometry in [("AT-1", box(0, 0, 2, 4)), ("AT-2", box(2, 0, 4, 4))]
        ]
    }
    data = {
        "features": [
            {
                "properties": {"shapeID": code, "shapeName": name, "shapeType": "ADM2"},
                "geometry": mapping(geometry),
            }
            for code, name, geometry in [
                ("a", "West", box(0, 0, 2, 2)),
                ("b", "North", box(0, 2, 2, 4)),
                ("c", "East", box(2, 0, 4, 4)),
            ]
        ]
    }
    metadata = {
        "boundaryISO": "AUT",
        "boundaryType": "ADM2",
        "boundaryID": "AUT-test",
        "boundaryLicense": "test",
        "simplifiedGeometryGeoJSON": "https://example.test/data",
    }
    return geography, metadata, data


def test_geometric_assignment_parents_boundary_and_outside():
    geography, metadata, data = fixture()
    dataset = SubregionDataset("AT", metadata, data, geography)
    assert dataset.locate(1, 1) == "gb:AT:ADM2:a"
    assert dataset.by_code[dataset.locate(3, 1)].region == "AT-1"
    assert dataset.by_code[dataset.locate(1, 3)].region == "AT-2"
    assert dataset.locate(10, 10) is None
    assert dataset.locate(2, 1) == "gb:AT:ADM2:a"  # Общая граница: ровно одна область.
    assert dataset.visible((2.1, 0.1, 3.9, 3.9)) == [2]


def test_subregion_support_does_not_invent_or_redistribute_mass():
    a = LocalPoint("a", 1, 1, "AT", "AT-1", 0.2)
    b = replace(a, id="b", latitude=3, mass=0.1)
    missing = replace(a, id="missing", mass=0.4)
    places = {"a": Subregion("a", "West", "AT", "AT-1"), "b": Subregion("b", "North", "AT", "AT-1")}
    assignments = {a.code: "a", b.code: "b"}
    result = subregion_support((a, b, missing), assignments, places, {"AT": 0.9}, {"AT-1": 0.8})
    assert result == {"a": 0.2, "b": 0.1}
    assert subregion_support((), assignments, places, {"AT": 0.9}, {"AT-1": 0.8}) == {}
    capped = subregion_support((a, b), assignments, places, {"AT": 0.1}, {"AT-1": 0.15})
    assert sum(capped.values()) == pytest.approx(0.1)
    assert capped["a"] / capped["b"] == pytest.approx(2)
    changed = replace(a, region="AT-2")
    assert subregion_support((changed,), {changed.code: "a"}, places, {"AT": 1}, {"AT-1": 1}) == {}


def test_subregion_cache_restart_corruption_and_offline_stale(tmp_path):
    geography, metadata, data = fixture()
    calls = []

    def fetch(url):
        calls.append(url)
        return data if url.endswith("data") else metadata

    repo = SubregionRepository(tmp_path, geography, fetch)
    assert len(repo.load("AT").places) == 3
    assert len(calls) == 2

    def offline(url):
        raise OSError("offline")

    repo = SubregionRepository(tmp_path, geography, offline)
    assert repo.load("AT").locate(1, 1) == "gb:AT:ADM2:a"
    path = tmp_path / "AUT-ADM2.json.gz"
    saved = json.loads(gzip.decompress(path.read_bytes()))
    saved["saved_at"] = 0
    path.write_bytes(gzip.compress(json.dumps(saved).encode()))
    assert repo.load("AT").metadata == metadata  # Старая версия доступна без интернета.
    path.write_bytes(b"corrupted")
    with pytest.raises(OSError, match="offline"):
        repo.load("AT")
    repaired = SubregionRepository(tmp_path, geography, fetch).load("AT")
    assert len(repaired.places) == 3


def test_cache_unwritable_does_not_hide_boundaries(tmp_path):
    geography, metadata, data = fixture()
    blocked = tmp_path / "file"
    blocked.write_text("keep")
    repo = SubregionRepository(blocked, geography, lambda url: data if url.endswith("data") else metadata)
    assert len(repo.load("AT").places) == 3
    assert blocked.read_text() == "keep"


def test_prepared_cache_skips_geometric_rebuild_and_invalidates_with_parent_geometry(tmp_path, monkeypatch):
    geography, metadata, data = fixture()
    repo = SubregionRepository(tmp_path, geography, lambda url: data if url.endswith("data") else metadata)
    initial = repo.load("AT")
    constructor = SubregionDataset.__init__

    def forbidden(*args, **kwargs):
        raise AssertionError("Повторная подготовка полигонов")

    monkeypatch.setattr(SubregionDataset, "__init__", forbidden)
    loaded = repo.load("AT")
    assert loaded.places == initial.places
    assert loaded.locate(1, 1) == initial.locate(1, 1)
    assert loaded.drawings.keys() == initial.drawings.keys()
    monkeypatch.setattr(SubregionDataset, "__init__", constructor)
    geography["features"][0]["properties"]["code"] = "AT-NEW"
    updated = repo.load("AT")
    assert updated.by_code[updated.locate(1, 1)].region == "AT-NEW"
    (tmp_path / "AUT-ADM2.json.prepared.gz").write_bytes(b"corrupt")
    assert repo.load("AT").places == updated.places


def test_unknown_country_or_wrong_admin_level_does_not_fallback_to_country_polygon(tmp_path):
    geography, metadata, data = fixture()
    repo = SubregionRepository(tmp_path, geography, lambda url: {**metadata, "boundaryType": "ADM1"})
    with pytest.raises(ValueError, match="нет кода"):
        repo.load("NOT-ISO")
    with pytest.raises(ValueError, match="недоступен"):
        repo.load("AT")
    data["features"][0]["properties"]["shapeType"] = "ADM1"
    with pytest.raises(ValueError, match="ADM2"):
        SubregionDataset("AT", metadata, data, geography)
