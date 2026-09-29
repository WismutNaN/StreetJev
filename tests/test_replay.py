import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from streetjev.application.session import AnalysisResult, Session
from streetjev.domain.evidence import Distribution, Frame, GeoEvidence
from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.storage import SqliteRoundRepository


def test_replay_leaves_original_database_intact(tmp_path):
    database = tmp_path / "rounds.sqlite3"
    catalog, _ = load_geography()
    repo = SqliteRoundRepository(database)
    session = Session(catalog, repo)
    for i, probability in enumerate((0.15, 0.92, 0.75)):
        countries = dict.fromkeys(catalog.countries, (1 - probability) / (len(catalog.countries) - 1))
        countries["SD"] = probability
        evidence = GeoEvidence("test", "v1", Distribution(countries), {}, "{}", 0)
        frame = Frame(str(i), b"", bytes([i]), "2026-09-30", "test")
        session.accept(AnalysisResult(session.round.id, frame, evidence, 1))
    saved = json.loads(repo.connection.execute("SELECT snapshot FROM rounds").fetchone()[0])
    saved["countries"]["SD"] = 0.9998
    saved.pop("aggregation")
    with repo.connection:
        repo.connection.execute("UPDATE rounds SET snapshot = ?", (json.dumps(saved),))
    repo.close()
    before = database.read_bytes()
    output = tmp_path / "replay.json"
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/replay_rounds.py"),
            "--database",
            str(database),
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result[0]["old_score"] == 0.9998
    assert result[0]["new_score"] == pytest.approx((0.15 + 0.92 + 0.75) / 3)
    assert result[0]["old_algorithm"] == "tempered_bayes_v1"
    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 3


def test_replay_fusion_and_primary_comparison(tmp_path):
    database = tmp_path / "fusion.sqlite3"
    catalog, _ = load_geography()
    repo = SqliteRoundRepository(database)
    session = Session(catalog, repo)
    probabilities = dict.fromkeys(catalog.countries, 0.0)
    probabilities.update(AT=0.9, DE=0.1)
    primary = GeoEvidence("imajev", "test", Distribution(probabilities), {}, "{}", 0)
    probabilities.update(AT=0.1, DE=0.9)
    other = replace(primary, source="chipoint", countries=Distribution(probabilities))
    frame = Frame("one", b"", b"test", "2026-09-30", "test")
    session.accept(AnalysisResult(session.round.id, frame, primary, 1))
    session.supplement(AnalysisResult(session.round.id, frame, other, 1))
    repo.close()
    before = database.read_bytes()
    for options, expected in (([], 0.62), (["--imajev-only"], 0.9)):
        output = tmp_path / "report.json"
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parents[1] / "scripts/replay_rounds.py"),
                "--database",
                str(database),
                "--output",
                str(output),
                *options,
            ],
            check=True,
            capture_output=True,
        )
        assert json.loads(output.read_text(encoding="utf-8"))[0]["new_score"] == pytest.approx(expected)
    assert database.read_bytes() == before
