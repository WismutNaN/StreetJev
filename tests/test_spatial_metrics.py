import math
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import evidence, frame

from streetjev.application import calibration
from streetjev.application.calibration import LabeledRound, fit, improves, measure, split_rounds
from streetjev.application.spatial_metrics import (
    DISTANCE_SCALE_KM,
    MISSING_DISTANCE_KM,
    has_coordinates,
    point_error,
    summarize_distances,
)
from streetjev.domain.calibration import CalibrationParameters
from streetjev.domain.ground_truth import GroundTruth
from streetjev.domain.localization import EARTH_RADIUS_KM, LocalPoint, distance_km
from streetjev.presentation.ranking import ranked_places


def snapshot(points=(), countries=None, known=1):
    return SimpleNamespace(
        points=points,
        countries=countries or {"AT": 0.6, "DE": 0.4},
        regions={},
        observed_regions=(),
        known_frames=known,
        active=bool(known),
    )


def sample(identity="a", truth=None, points=(), payload=None):
    return LabeledRound(
        identity,
        truth or GroundTruth(0, 0, "AT"),
        ((frame(identity=identity), 1, replace(evidence(), points=points), payload),),
    )


def test_great_circle_distance_edges():
    assert distance_km(47, 11, 47, 11) == 0
    assert distance_km(0, 0, 0, 90) == pytest.approx(math.pi * EARTH_RADIUS_KM / 2)
    assert distance_km(0, 0, 0, 180) == pytest.approx(MISSING_DISTANCE_KM)
    assert distance_km(0, 179, 0, -179) == pytest.approx(222.39016)
    assert distance_km(0, -179, 0, 179) == pytest.approx(distance_km(0, 179, 0, -179))
    assert distance_km(80, 179, 80, -179) < distance_km(0, 179, 0, -179)
    assert distance_km(90, 0, 90, 180) == pytest.approx(0, abs=1e-9)


def test_selection_matches_map_and_does_not_use_truth(catalog):
    points = (
        LocalPoint("near", 0, 0, "AT", None, 0.01),
        LocalPoint("selected", 0, 1, "AT", None, 0.2),
        LocalPoint("global", 0, 10, "DE", None, 0.3),
        *(LocalPoint(str(i), 0, 20 + i, "DE", None, 0.02) for i in range(4)),
        LocalPoint("zero", 0, 0, "AT", None, 0),
    )
    result = snapshot(points)
    error = point_error(result, GroundTruth(0, 0, "AT"))
    ui_point = next(row for row in ranked_places(catalog, result) if row.kind == "point")
    assert ui_point.code == points[1].code
    assert error["selected_point"]["id"] == "selected"
    assert error["distance_km"] == pytest.approx(111.19508)
    assert error["nearest_candidate_km"] == 0
    assert error["top5_distance_km"] == error["distance_km"]
    moved_truth = point_error(result, GroundTruth(0, 10, "DE"))
    assert moved_truth["selected_point"] == error["selected_point"]
    assert moved_truth["distance_km"] > error["distance_km"]


def test_missing_prediction_is_penalized_and_source_eligibility_is_fixed():
    elsewhere = LocalPoint("other", 0, 0, "DE", None, 0.4)
    missing = point_error(snapshot((elsewhere,)), GroundTruth(0, 0, "AT"))
    assert missing["distance_km"] is None
    assert missing["nearest_candidate_km"] == 0
    assert missing["log_distance"] == pytest.approx(math.log1p(MISSING_DISTANCE_KM / DISTANCE_SCALE_KM))
    assert point_error(snapshot((elsewhere,), known=0), GroundTruth(0, 0, "DE"))["distance_km"] is None
    assert has_coordinates(sample(points=(elsewhere,)))
    assert has_coordinates(sample(payload={"candidates": [{"lat": 0, "lon": 0}]}))
    assert not has_coordinates(sample(payload={"candidates": []}))
    assert not has_coordinates(sample())
    metrics = measure([sample(points=(elsewhere,))], CalibrationParameters(), lambda *_: snapshot((elsewhere,)))
    assert metrics["point_rounds"] == 1
    assert metrics["point_predictions"] == 0
    assert metrics["point_distance_mean_km"] is None
    assert metrics["point_within_100km"] == 0
    assert metrics["point_log_distance"] == missing["log_distance"]


def test_summary_counts_missing_and_zero_distance():
    rows = [
        {
            "distance_km": value,
            "log_distance": math.log1p((value if value is not None else MISSING_DISTANCE_KM) / 25),
            "nearest_candidate_km": 0,
            "top5_distance_km": 0,
        }
        for value in (0, 100, None)
    ]
    result = summarize_distances(rows)
    assert result["point_prediction_coverage"] == pytest.approx(2 / 3)
    assert result["point_distance_mean_km"] == result["point_distance_median_km"] == 50
    assert result["point_distance_p90_km"] == 90
    assert result["point_within_25km"] == pytest.approx(1 / 3)
    assert result["point_within_100km"] == pytest.approx(2 / 3)
    assert result["candidate_within_25km"] == 1
    assert result["point_log_distance"] == pytest.approx(sum(row["log_distance"] for row in rows) / 3)
    empty = summarize_distances([])
    assert empty["point_rounds"] == empty["point_predictions"] == 0
    assert empty["point_distance_median_km"] is None
    assert empty["point_within_100km"] is None
    assert summarize_distances(rows[:1])["point_distance_p90_km"] == 0


def test_geographic_fit_uses_training_distances_only(monkeypatch):
    # В обоих вариантах страна верна с одинаковой оценкой; отличаются только координаты.
    samples = [
        sample(str(i), GroundTruth(i, 0, "AT"), (LocalPoint(str(i), i, 1, "AT", None, 1),)) for i in range(12)
    ]

    def predict(row, parameters):
        point = row.observations[0][2].points[0]
        if parameters.secondary_weight == 0.8:
            point = replace(point, longitude=0)
        return snapshot((point,))

    monkeypatch.setattr(calibration, "GRID", {"secondary_weight": (0.35, 0.8)})
    best, report = fit(samples, predict, objective="geographic")
    assert best.secondary_weight == 0.8
    assert report["eligible_to_apply"]
    assert report["validation"]["candidate"]["point_distance_median_km"] == 0
    metrics = report["validation"]["baseline"]
    assert metrics["loss"] == pytest.approx((metrics["classification_loss"] + metrics["point_log_distance"]) / 2)
    assert report["validation_details"]
    _, validation = split_rounds(samples)
    ids = {s.id for s in validation}
    changed = [replace(s, truth=replace(s.truth, longitude=0.2)) if s.id in ids else s for s in samples]
    assert fit(changed, predict, objective="geographic")[0] == best
    categorical, _ = fit(samples, predict)
    assert categorical.secondary_weight == 0.35
    # Без координатного источника сохраняется обычная классификационная цель.
    no_source = measure([sample()], CalibrationParameters(), lambda *_: snapshot(), objective="geographic")
    assert no_source["loss"] == no_source["classification_loss"]
    assert no_source["point_rounds"] == 0


@pytest.mark.parametrize(
    "metric,value",
    [
        ("point_distance_mean_km", 101),
        ("point_distance_median_km", 51),
        ("point_log_distance", 3.1),
        ("point_prediction_coverage", 0.9),
        ("point_rounds", 9),
        ("point_distance_mean_km", None),
    ],
)
def test_acceptance_rejects_spatial_regression(metric, value):
    baseline = {
        "loss": 3,
        "country_accuracy": 0.75,
        "region_accuracy": None,
        "point_rounds": 10,
        "point_prediction_coverage": 1,
        "point_distance_mean_km": 100,
        "point_distance_median_km": 50,
        "point_log_distance": 3,
    }
    better = {**baseline, "loss": 2}
    assert improves(better, baseline)
    assert not improves({**better, metric: value}, baseline)
