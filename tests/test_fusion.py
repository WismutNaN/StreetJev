import json
import sqlite3
from dataclasses import replace

import pytest
from conftest import evidence, frame

from streetjev.application.session import AnalysisResult, Session
from streetjev.domain.evidence import Distribution
from streetjev.domain.fusion import fuse_evidence
from streetjev.infrastructure.storage import SqliteRoundRepository


def secondary(at=0.1, **kwargs):
    return replace(evidence(at=at, **kwargs), source="chipoint")


def test_disagreement_stays_broad_and_region_does_not_promote_country():
    result = fuse_evidence(evidence(at=0.9), secondary(at=0.1))
    assert result.countries.probabilities["AT"] == pytest.approx(0.62)
    assert result.countries.unknown == 0
    agree = fuse_evidence(evidence(at=0.9), secondary(at=0.9))
    assert agree.countries.probabilities["AT"] == pytest.approx(0.9)


def test_unknown_expert_does_not_move_known_country():
    result = fuse_evidence(evidence(), secondary(at=0.1, unknown=1))
    assert result.countries.probabilities["AT"] == pytest.approx(0.8)
    assert result.countries.unknown == pytest.approx(0.35)
    assert fuse_evidence(evidence(unknown=1), secondary(unknown=1)).countries.unknown == 1


def test_conditional_regions_do_not_repeat_country_vote():
    primary = evidence(at=0.8, regions={"AT": Distribution({"AT-7": 0.9, "AT-5": 0.1})})
    other = secondary(at=0.2, regions={"AT": Distribution({"AT-7": 0.1, "AT-5": 0.9})})
    result = fuse_evidence(primary, other)
    assert result.regions["AT"].probabilities["AT-7"] == pytest.approx(0.62)
    # Даже ветвь страны с исходным нулём участвует после проверки по подсказке Chipoint.
    primary = replace(primary, countries=Distribution({"AT": 0, "DE": 1}))
    assert fuse_evidence(primary, other).regions["AT"].probabilities["AT-7"] == pytest.approx(0.62)


def test_late_result_replaces_same_frame_and_does_not_become_last(catalog, tmp_path):
    repo = SqliteRoundRepository(tmp_path / "rounds.sqlite3")
    session = Session(catalog, repo)
    first, last = frame(), frame(255, "last")
    session.accept(AnalysisResult(session.round.id, first, evidence(at=0.9), 1))
    session.accept(AnalysisResult(session.round.id, last, evidence(at=0.7), 1))
    late = AnalysisResult(session.round.id, first, secondary(), 1)
    assert session.supplement(late)
    snapshot = session.round.snapshot()
    assert snapshot.frames == 2
    assert snapshot.countries["AT"] == pytest.approx((0.62 + 0.7) / 2)
    assert snapshot.last_countries["AT"] == pytest.approx(0.7)
    assert not session.supplement(late)
    assert repo.connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 2
    assert repo.connection.execute("SELECT count(*) FROM supplementary_observations").fetchone()[0] == 1
    # Сырые ответы основного эксперта не заменяются fusion.
    primary = json.loads(
        repo.connection.execute("SELECT evidence FROM observations WHERE frame_id=?", (first.id,)).fetchone()[0]
    )
    assert primary["countries"]["AT"] == pytest.approx(0.9)
    session.clear_supplements()
    assert session.round.countries["AT"] == pytest.approx(0.8)
    assert session.round.frames == 2
    assert repo.connection.execute("SELECT active FROM supplementary_observations").fetchone()[0] == 0
    assert not session.supplement(late)
    session.reset()
    assert not session.supplement(late)
    repo.close()


def test_supplement_failure_is_atomic(catalog, tmp_path):
    repo = SqliteRoundRepository(tmp_path / "rounds.sqlite3")
    session = Session(catalog, repo)
    primary = AnalysisResult(session.round.id, frame(), evidence(), 1)
    assert session.accept(primary)
    before = session.round.snapshot()
    repo.connection.execute("PRAGMA query_only=ON")
    with pytest.raises(sqlite3.OperationalError):
        session.supplement(replace(primary, evidence=secondary()))
    assert session.round.snapshot() == before
    assert not session._supplements
    repo.close()
