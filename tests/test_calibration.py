import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import frame

from streetjev.application.calibration import LabeledRound, fit, improves, split_rounds
from streetjev.application.session import AnalysisResult, Session
from streetjev.domain.calibration import CalibrationParameters, CalibrationProfile, adjust_imajev, temperature
from streetjev.domain.clues import PlaceClues, apply_clues
from streetjev.domain.evidence import Distribution
from streetjev.domain.ground_truth import GroundTruth
from streetjev.infrastructure.calibration import (
    CalibrationReplay,
    profile_data,
    read_labels,
    read_profile,
    write_json,
)
from streetjev.infrastructure.chipoint import ChipointExpert
from streetjev.infrastructure.coverage import CoverageDensity
from streetjev.infrastructure.imajev import ImajevExpert
from streetjev.infrastructure.storage import SqliteRoundRepository


def answer(values, unknown=0):
    return {"type": "choice", "probabilities": values, "unknown_probability": unknown}


def raw(at=0.99, model="imajev-test"):
    return json.dumps(
        {
            "country": {"model": model, "answers": {"country": answer({"AT": at, "DE": 1 - at})}},
            "regions": {
                "answers": {
                    "region_AT": answer({"AT-7": 0.95, "AT-5": 0.05}),
                    "region_DE": answer({"DE-BY": 0.8, "DE-HE": 0.2}),
                }
            },
        }
    )


def sample_rounds(catalog):
    result = []
    expert = ImajevExpert(catalog)
    # Каждый раунд переуверен в AT; истина чередуется. В каждой ячейке две разные страны.
    # Геометрия тут синтетическая: проверяем разделение и статистику, не локатор.
    for group in range(12):
        for variant, country, region in ((0, "AT", "AT-7"), (1, "DE", "DE-BY")):
            identity = f"{group}-{variant}"
            truth = GroundTruth(group, 11 + variant / 100, country, region)
            observation = (frame(identity=identity), 1, expert.decode(raw()), None)
            result.append(LabeledRound(identity, truth, (observation,)))
    return result


def test_temperatures_and_profile_version_guard(catalog, tmp_path):
    original = Distribution({"AT": 0.99, "DE": 0.01}, 0.2)
    adjusted = temperature(original, 4)
    assert adjusted.unknown == 0.2
    assert 0.5 < adjusted.probabilities["AT"] < 0.8
    parameters = CalibrationParameters(imajev_country_temperature=3, secondary_weight=0.7)
    profile = CalibrationProfile(parameters, ("a",), ("b",))
    path = tmp_path / "calibration.json"
    write_json(path, profile_data(profile, catalog))
    assert read_profile(path, catalog) == profile
    assert profile.for_model("imajev", "wrong") == CalibrationParameters()
    assert profile.for_model("imajev", "a") == parameters
    assert profile.fusion_weight("a", "b") == 0.7
    assert profile.fusion_weight("wrong", "b") == 0.35
    data = profile_data(profile, catalog)
    data["catalog_hash"] = "other"
    write_json(path, data)
    with pytest.raises(ValueError, match="каталог"):
        read_profile(path, catalog)
    with pytest.raises(ValueError):
        CalibrationParameters(imajev_country_temperature=float("nan"))


def test_grouped_split_and_fitting_calibrates_overconfidence(catalog):
    samples = sample_rounds(catalog)
    training, validation = split_rounds(samples)
    assert not {s.group for s in training} & {s.group for s in validation}
    assert split_rounds(samples) == (training, validation)
    replay = CalibrationReplay(catalog, {"features": []})
    replay.imajev_models = {"imajev-test"}
    best, report = fit(samples, replay)
    assert best.imajev_country_temperature > 1
    assert report["eligible_to_apply"]
    assert (
        report["validation"]["candidate"]["country_log_loss"]
        < report["validation"]["baseline"]["country_log_loss"]
    )
    assert report["validation"]["candidate"]["country_accuracy"] == 0.5
    assert report["validation"]["candidate"]["country_top5_accuracy"] == 1
    assert report["validation"]["candidate"]["region_top5_accuracy"] == 1
    assert best.secondary_weight == 0.35  # Нет второго эксперта: его вес не меняется.
    # Изменение только проверочных меток не должно менять подобранные параметры.
    validation_ids = {s.id for s in validation}
    changed = [
        replace(s, truth=replace(s.truth, country="AT", region="AT-7")) if s.id in validation_ids else s
        for s in samples
    ]
    second, _ = fit(changed, replay)
    assert second == best
    with pytest.raises(ValueError, match="12"):
        fit(samples[:4], replay)
    assert not improves(
        {"loss": 1, "country_accuracy": 0.4, "region_accuracy": None},
        {"loss": 2, "country_accuracy": 0.5, "region_accuracy": None},
    )


def test_fresh_holdout_never_reuses_old_locations(catalog):
    samples = sample_rounds(catalog)
    fresh_ids = {sample.id for sample in samples[8:]}
    # Новый раунд в уже использованной географической ячейке остаётся в training.
    fresh_ids.add(samples[1].id)
    training, validation = split_rounds(samples, validation_pool=fresh_ids)
    assert len({s.group for s in validation}) == 4
    assert {s.id for s in validation} <= fresh_ids
    assert samples[1] in training
    assert not {s.group for s in validation} & {s.group for s in training}
    assert split_rounds(samples, validation_pool=fresh_ids) == (training, validation)
    with pytest.raises(ValueError, match="4 новые"):
        split_rounds(samples, validation_pool={s.id for s in samples[-6:]})
    # При недостатке новых мест старые не подмешиваются в проверку.
    with pytest.raises(ValueError, match="4 новые"):
        split_rounds(samples, validation_pool={s.id for s in samples[1::2]})


def test_improvement_must_preserve_candidate_recall():
    baseline = {
        "loss": 3,
        "country_accuracy": 0.75,
        "region_accuracy": 0,
        "country_top5_accuracy": 1,
        "region_top5_accuracy": 0.25,
    }
    improved = {**baseline, "loss": 2}
    assert improves(improved, baseline)
    assert not improves({**improved, "country_top5_accuracy": 0.875}, baseline)
    assert not improves({**improved, "region_top5_accuracy": 0.125}, baseline)


def test_raw_replay_matches_live_profile_and_keeps_db_readonly(catalog, tmp_path):
    geography = {
        "features": [
            {
                "properties": {"code": "AT"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[10, 45], [17, 45], [17, 50], [10, 50], [10, 45]]],
                },
            }
        ]
    }
    replay = CalibrationReplay(catalog, geography)
    parameters = CalibrationParameters(
        imajev_country_temperature=2,
        imajev_region_temperature=3,
        chipoint_temperature=4,
        secondary_weight=0.6,
        driving_strength=0.2,
        coverage_penalty=0.3,
    )
    profile = CalibrationProfile(parameters, ("imajev-test",), ("chip-test",))
    original = json.loads(raw())
    original["country"]["answers"]["driving_side"] = answer({"left": 0.9, "right": 0.05, "unclear": 0.05})
    original["chipoint_region_refinement"] = {"answers": {"region_DE": answer({"DE-BY": 0.7, "DE-HE": 0.3})}}
    primary_raw = ImajevExpert(catalog).decode(json.dumps(original))
    replay.facts = {"AT": PlaceClues("right"), "DE": PlaceClues("left")}
    primary = adjust_imajev(primary_raw, parameters, replay.facts)
    x, y = CoverageDensity.tile(48, 12)
    payload = {
        "schema": "streetjev.chipoint.v1",
        "mode": "street_retrieval",
        "model": "chip-test",
        "candidates": [
            {"id": "at", "lat": 48, "lon": 12, "score": 2},
            {"id": "ocean", "lat": 0, "lon": -140, "score": 1},
        ],
        "coverage": {"source": "google_svv", "zoom": 12, "tiles": [{"x": x, "y": y, "density": 0}]},
    }
    secondary = ChipointExpert(catalog, geography, profile=profile).convert(payload, 0)
    secondary = apply_clues(
        secondary, primary.clues, replay.facts, parameters.driving_strength, parameters.hemisphere_strength
    )
    path = tmp_path / "rounds.sqlite3"
    repo = SqliteRoundRepository(path)
    session = Session(catalog, repo, profile)
    result = AnalysisResult(session.round.id, frame(), primary, 1)
    session.accept(result)
    session.supplement(replace(result, evidence=secondary))
    session.label(GroundTruth(48, 12, "AT", "AT-7"))
    expected = session.round.snapshot()
    repo.close()
    before = path.read_bytes()
    samples, skipped = replay.prepare(read_labels(path))
    assert not skipped and len(samples) == 1
    actual = replay(samples[0], parameters)
    assert actual.countries == pytest.approx(expected.countries)
    assert actual.regions == pytest.approx(expected.regions)
    assert actual.cells == expected.cells
    assert actual.points == expected.points
    assert actual.effective_frames == pytest.approx(expected.effective_frames)
    assert actual.frames == expected.frames
    assert path.read_bytes() == before
    repo = SqliteRoundRepository(path)
    repo.disable_supplements(expected)
    repo.close()
    assert read_labels(path)[0]["observations"][0]["secondary"] is None


def test_prepare_excludes_demo_and_selects_one_model_version(catalog):
    replay = CalibrationReplay(catalog, {"features": []})

    def row(identity, model="imajev-test", expert="imajev_hierarchical"):
        return {
            "id": identity,
            "truth": GroundTruth(47, 11, "AT"),
            "observations": [
                {
                    "frame_id": identity,
                    "captured_at": "today",
                    "source": "test",
                    "novelty": 1,
                    "elapsed_ms": 0,
                    "expert": expert,
                    "model_version": model,
                    "raw_json": raw(model=model),
                    "secondary": None,
                }
            ],
        }

    samples, skipped = replay.prepare([row("1"), row("2"), row("3", "old"), row("4", expert="demo")])
    assert [s.id for s in samples] == ["1", "2"]
    assert len(skipped) == 2
    samples, _ = replay.prepare([row("1"), row("3", "old")], imajev_model="old")
    assert [s.id for s in samples] == ["3"]


def test_cli_report_apply_backup_and_no_replacement_without_improvement(catalog, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "calibrate_script", Path(__file__).parents[1] / "scripts/calibrate.py"
    )
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    monkeypatch.setattr(command, "load_geography", lambda: (catalog, {"features": []}))
    path = tmp_path / "rounds.sqlite3"
    active = tmp_path / "calibration.json"
    repo = SqliteRoundRepository(path)
    session = Session(catalog, repo)
    samples = sample_rounds(catalog)
    # Недостаточно данных: даже --apply не создаёт профиль.
    with pytest.raises(ValueError, match="12"):
        command.calibrate(path, tmp_path / "reports", apply=True, progress=None)
    assert not active.exists()
    for sample in samples:
        captured, novelty, evidence, _ = sample.observations[0]
        session.accept(AnalysisResult(session.round.id, captured, evidence, novelty))
        session.label(sample.truth)
        session.reset()
    repo.close()
    before = path.read_bytes()
    output = tmp_path / "reports"
    report = command.calibrate(path, output, progress=None, objective="geographic")
    assert report["eligible_to_apply"] and not report["applied"]
    assert report["objective"] == "geographic"
    assert report["validation"]["candidate"]["point_rounds"] == 0
    assert not active.exists()
    assert (output / "report.json").is_file() and (output / "profile.json").is_file()
    old = CalibrationProfile(CalibrationParameters(), ("imajev-test",))
    write_json(active, profile_data(old, catalog))
    report = command.calibrate(path, output, apply=True, progress=None)
    assert report["applied"]
    assert read_profile(active, catalog).parameters.imajev_country_temperature > 1
    assert read_profile(tmp_path / "calibration.previous.json", catalog) == old
    saved = active.read_bytes()
    report = command.calibrate(path, output, apply=True, progress=None)
    assert not report["applied"]  # Та же калибровка не считается новым улучшением.
    assert active.read_bytes() == saved
    assert path.read_bytes() == before
    previous = output / "report.json"
    old_report = previous.read_bytes()
    with pytest.raises(ValueError, match="прежний"):
        command.calibrate(path, output, previous_report=previous, progress=None)
    with pytest.raises(ValueError, match="4 новые"):
        command.calibrate(path, tmp_path / "next", apply=True, previous_report=previous, progress=None)
    assert previous.read_bytes() == old_report and active.read_bytes() == saved
