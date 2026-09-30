from dataclasses import replace

import pytest
from conftest import evidence, frame

from streetjev.domain.clues import PlaceClues, apply_clues
from streetjev.domain.evidence import Distribution
from streetjev.domain.localization import LocalPoint, combine_points, local_scores
from streetjev.domain.round import GeoRound
from streetjev.domain.subregions import Subregion
from streetjev.presentation.map_geometry import cluster_points, project, unproject, zoom_bucket
from streetjev.presentation.ranking import ranked_places, row_score
from streetjev.presentation.scores import display_scores


def test_points_keep_coordinates_and_parent_caps():
    a = LocalPoint("a", 47.26921, 11.40417, "AT", "AT-7", 0.5)
    b = replace(a, id="b", latitude=47.26922, mass=0.3)
    points = combine_points((a, a, b))
    assert len(points) == 2 and points[0].mass == 1
    assert points[0].longitude == 11.40417
    assert replace(a, latitude=47).code == replace(a, latitude=47.0).code
    scaled = local_scores(points, 2, {"AT": 0.1}, {"AT-7": 0.05})
    assert sum(p.mass for p in scaled) == pytest.approx(0.05)
    for field, value in (("latitude", 91), ("longitude", 181), ("mass", float("nan")), ("id", "")):
        with pytest.raises(ValueError):
            replace(a, **{field: value})


def test_clues_affect_spatial_support_without_moving_points():
    point = LocalPoint("a", 47.26921, 11.40417, "AT", "AT-7", 0.5)
    base = replace(evidence(), points=(point,))
    result = apply_clues(
        base, {"driving_side": Distribution({"left": 1, "right": 0})}, {"AT": PlaceClues("right")}
    )
    assert result.points[0] == replace(point, mass=0.5 * 0.55)


def test_zoom_clusters_conserve_mass_and_never_cross_parents_or_date_line():
    points = (
        LocalPoint("a", 47.26921, 11.40417, "AT", "AT-7", 0.2),
        LocalPoint("b", 47.26921, 11.40427, "AT", "AT-7", 0.1),
    )
    # Совпадения не превращаются в центры десятикилометровых ячеек.
    low = cluster_points(points, zoom_bucket(20))
    high = cluster_points(points, zoom_bucket(400000))
    assert len(low) == 1 and len(high) == 2
    assert sum(g.mass for g in low) == pytest.approx(sum(g.mass for g in high))
    assert high[0].points[0].longitude in (11.40417, 11.40427)
    assert len(cluster_points((points[0], replace(points[1], region="AT-5")), zoom_bucket(20))) == 2
    edges = (replace(points[0], longitude=179.99), replace(points[1], longitude=-179.99))
    assert len(cluster_points(edges, zoom_bucket(1))) == 2
    assert unproject(*project(11.40417, 47.26921)) == pytest.approx((11.40417, 47.26921))


def test_hierarchy_has_all_levels_and_missing_local_last_frame(catalog):
    state = GeoRound(catalog)
    point = LocalPoint("a", 47.26921, 11.40417, "AT", "AT-7", 0.3)
    sample = replace(evidence(at=0.8, regions={"AT": Distribution({"AT-7": 0.8, "AT-5": 0.2})}), points=(point,))
    state.accept(state.id, frame(), sample, 1)
    scores = display_scores(state.snapshot())
    rows = ranked_places(catalog, scores)
    assert [row.kind for row in rows[:3]] == ["country", "region", "point"]
    assert rows[2].code == point.code
    assert row_score(rows[2], scores) == pytest.approx(0.3)
    state.accept(state.id, frame(255, "new"), evidence(), 1)
    assert row_score(rows[2], display_scores(state.snapshot(), "last")) is None
    assert row_score(rows[2], display_scores(state.snapshot())) == pytest.approx(0.15)


def test_hierarchy_includes_real_admin2_and_keeps_missing_evidence_unknown(catalog):
    state = GeoRound(catalog)
    point = LocalPoint("a", 47.2692, 11.4041, "AT", "AT-7", 0.3)
    sample = replace(evidence(at=0.8, regions={"AT": Distribution({"AT-7": 0.8, "AT-5": 0.2})}), points=(point,))
    state.accept(state.id, frame(), sample, 1)
    scores = display_scores(state.snapshot())
    places = {"district": Subregion("district", "Innsbruck-Stadt", "AT", "AT-7")}
    rows = ranked_places(catalog, scores, places, {"district": 0.3}, {point.code: "district"})
    assert [row.kind for row in rows[:4]] == ["country", "region", "subregion", "point"]
    assert rows[2].label.strip() == "Innsbruck-Stadt"
    assert row_score(rows[2], scores, {"district": 0.3}) == 0.3
    assert row_score(rows[2], scores, {}) is None
    assert not any(row.kind == "subregion" for row in ranked_places(catalog, scores, places, {}))
