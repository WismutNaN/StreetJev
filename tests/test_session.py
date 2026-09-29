import json
import sqlite3

import pytest
from conftest import evidence, frame

from streetjev.application.session import AnalysisResult, Session
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
