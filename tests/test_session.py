import json
import sqlite3
from dataclasses import replace

import pytest
from conftest import evidence, frame

from streetjev.application.session import AnalysisResult, Session
from streetjev.domain.evidence import Distribution
from streetjev.infrastructure.storage import SqliteRoundRepository


def test_persistence_reset_and_late_reply(catalog, tmp_path):
    repo = SqliteRoundRepository(tmp_path / "rounds.sqlite3")
    session = Session(catalog, repo)
    old_id = session.round.id
    result = AnalysisResult(old_id, frame(), evidence(), 1)
    assert session.accept(result)
    stored = repo.connection.execute("SELECT snapshot FROM rounds WHERE id = ?", (old_id,)).fetchone()
    assert json.loads(stored[0])["frames"] == 1
    assert json.loads(stored[0])["aggregation"] == "weighted_linear_v2"
    assert json.loads(stored[0])["last_countries"]["AT"] == pytest.approx(0.8)
    session.reset()
    assert not session.accept(result)
    assert session.round.frames == 0
    assert repo.connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
    assert repo.connection.execute("SELECT count(*) FROM rounds").fetchone()[0] == 2
    repo.close()


def test_repository_failure_does_not_publish_unsaved_state(catalog, tmp_path):
    repo = SqliteRoundRepository(tmp_path / "rounds.sqlite3")
    session = Session(catalog, repo)
    repo.connection.execute("PRAGMA query_only=ON")
    with pytest.raises(sqlite3.OperationalError):
        session.accept(AnalysisResult(session.round.id, frame(), evidence(), 1))
    assert session.round.frames == 0
    assert session.round.novelty(frame()) == 1
    repo.close()


def test_refinement_is_persisted_once_on_original_frame_and_survives_chipoint_toggle(catalog, tmp_path):
    repo = SqliteRoundRepository(tmp_path / "rounds.sqlite3")
    session = Session(catalog, repo)
    primary = AnalysisResult(session.round.id, frame(), evidence(at=0.1), 1)
    session.accept(primary)
    session.supplement(replace(primary, evidence=replace(evidence(at=0.9), source="chipoint")))
    session.accept(AnalysisResult(session.round.id, frame(255, "next"), evidence(at=0.7), 1))
    refined = replace(
        primary,
        evidence=replace(
            primary.evidence,
            regions={"AT": Distribution({"AT-7": 0.9, "AT-5": 0.1})},
            raw_json='{"raw": true, "chipoint_region_refinement": {}}',
        ),
    )
    assert session.refine(refined)
    assert session.round.frames == 2
    assert session.round.snapshot().last_countries["AT"] == pytest.approx(0.7)
    assert "AT" in session.round.observed_regions
    row = repo.connection.execute(
        "SELECT evidence, raw_json FROM observations WHERE frame_id=?", (primary.frame.id,)
    ).fetchone()
    assert "AT" in json.loads(row[0])["regions"]
    assert json.loads(row[1])["raw"] is True
    assert not session.refine(refined)
    session.clear_supplements()
    assert session.round.countries["AT"] == pytest.approx(0.4)
    assert session.round.snapshot().observed_regions == {"AT"}
    session.reset()
    assert not session.refine(refined)
    repo.close()


def test_refinement_rejects_country_change_and_failed_save_is_atomic(catalog, tmp_path):
    repo = SqliteRoundRepository(tmp_path / "rounds.sqlite3")
    session = Session(catalog, repo)
    primary = AnalysisResult(session.round.id, frame(), evidence(), 1)
    session.accept(primary)
    regions = {"DE": Distribution({"DE-BY": 0.8, "DE-HE": 0.2})}
    assert not session.refine(replace(primary, evidence=evidence(at=0.1, regions=regions)))
    before = session.round.snapshot()
    repo.connection.execute("PRAGMA query_only=ON")
    with pytest.raises(sqlite3.OperationalError):
        session.refine(replace(primary, evidence=replace(primary.evidence, regions=regions)))
    assert session.round.snapshot() == before
    assert session.observation(primary.frame.id).evidence.regions == {}
    repo.close()
