import sqlite3

import pytest
from conftest import evidence, frame

from streetjev.application.session import AnalysisResult, Session
from streetjev.domain.ground_truth import GroundTruth
from streetjev.infrastructure.storage import SqliteRoundRepository


def test_label_correct_reset_and_do_not_change_prediction(catalog, tmp_path):
    repo = SqliteRoundRepository(tmp_path / "rounds.sqlite3")
    session = Session(catalog, repo)
    with pytest.raises(ValueError, match="кадр"):
        session.label(GroundTruth(47, 11, "AT", "AT-7"))
    session.accept(AnalysisResult(session.round.id, frame(), evidence(), 1))
    before = session.round.snapshot()
    session.label(GroundTruth(47, 11, "AT", "AT-7"))
    assert session.round.snapshot() == before
    assert not session.accept(AnalysisResult(session.round.id, frame(255, "later"), evidence(), 1))
    session.label(GroundTruth(48, 12, "DE", "DE-BY"))
    row = repo.connection.execute("SELECT country, region FROM ground_truth").fetchall()
    assert row == [("DE", "DE-BY")]
    session.reset()
    assert session.truth is None and session.round.frames == 0
    assert repo.connection.execute("SELECT count(*) FROM ground_truth").fetchone()[0] == 1
    repo.close()


def test_invalid_or_failed_label_is_atomic(catalog, tmp_path):
    repo = SqliteRoundRepository(tmp_path / "rounds.sqlite3")
    session = Session(catalog, repo)
    session.accept(AnalysisResult(session.round.id, frame(), evidence(), 1))
    with pytest.raises(ValueError):
        session.label(GroundTruth(47, 11, "AT", "DE-BY"))
    with pytest.raises(ValueError):
        GroundTruth(float("nan"), 11, "AT")
    repo.connection.execute("PRAGMA query_only=ON")
    with pytest.raises(sqlite3.OperationalError):
        session.label(GroundTruth(47, 11, "AT"))
    assert session.truth is None
    repo.close()
