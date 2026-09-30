import json
from dataclasses import replace

import pytest
from conftest import evidence, frame

from streetjev.application.session import AnalysisResult, Session
from streetjev.domain.evidence import Distribution
from streetjev.domain.fusion import fuse_evidence
from streetjev.domain.localization import LocalPoint, combine_cells, local_scores, locate_cell
from streetjev.domain.round import GeoRound
from streetjev.infrastructure.storage import SqliteRoundRepository


def test_grid_dimensions_and_edges():
    for lat in (-89.9, -40, 0, 48, 80, 89.9):
        cell = locate_cell(lat, 12, "AT", None, 0.5)
        west, south, east, north = cell.bounds
        assert south <= lat <= north and west <= 12 <= east
        assert (north - south) * 111.195 == pytest.approx(10, abs=0.02)
        assert locate_cell(*cell.center, "AT", None, 0.5).code == cell.code
    assert locate_cell(90, 180, "AT", None, 1).bounds[2] <= 180
    assert locate_cell(-90, -180, "AT", None, 1).bounds[1] == -90
    with pytest.raises(ValueError):
        locate_cell(91, 0, "AT", None, 1)


def test_close_points_combine_and_joint_mass_is_capped():
    cell = locate_cell(47, 11, "AT", "AT-7", 0.5)
    more = locate_cell(*cell.center, "AT", "AT-7", 0.3)
    other = locate_cell(48, 12, "AT", None, 0.2)
    cells = combine_cells((cell, more, other))
    assert len(cells) == 2
    assert cells[0].mass == pytest.approx(0.8)
    result = local_scores(cells, 1, {"AT": 0.1}, {"AT-7": 0.05})
    assert sum(c.mass for c in result) <= 0.1
    assert sum(c.mass for c in result if c.region == "AT-7") <= 0.05


def test_localization_fusion_novelty_reset_and_disable(catalog, tmp_path):
    primary = evidence(at=0.7, regions={"AT": Distribution({"AT-7": 0.8, "AT-5": 0.2})})
    secondary = replace(
        evidence(at=0.9, regions=primary.regions),
        source="chipoint",
        cells=(locate_cell(47.2, 11.3, "AT", "AT-7", 0.9),),
        points=(LocalPoint("a", 47.2345678, 11.3456789, "AT", "AT-7", 0.9),),
    )
    fused = fuse_evidence(primary, secondary)
    assert fused.cells[0].mass == pytest.approx(0.9 * 0.35)
    assert fused.points[0].mass == pytest.approx(0.9 * 0.35)
    repo = SqliteRoundRepository(tmp_path / "rounds.sqlite3")
    session = Session(catalog, repo)
    first = AnalysisResult(session.round.id, frame(), primary, 1)
    session.accept(first)
    assert not session.round.snapshot().cells
    session.supplement(replace(first, evidence=secondary))
    one = session.round.snapshot().cells[0].mass
    second = AnalysisResult(session.round.id, frame(255, "two"), primary, 0.5)
    session.accept(second)
    assert not session.round.snapshot().last_cells
    assert not session.round.snapshot().last_points
    session.supplement(replace(second, evidence=secondary))
    snapshot = session.round.snapshot()
    assert snapshot.frames == 2 and snapshot.cells[0].mass == pytest.approx(one)
    assert snapshot.points[0].mass == pytest.approx(one)
    assert snapshot.points[0].latitude == 47.2345678
    assert len(snapshot.points) == 1
    assert not session.supplement(replace(second, evidence=secondary))
    data = json.loads(
        repo.connection.execute("SELECT snapshot FROM rounds WHERE id=?", (session.round.id,)).fetchone()[0]
    )
    assert data["cells"][0]["mass"] == pytest.approx(one)
    assert data["points"][0]["longitude"] == 11.3456789
    session.clear_supplements()
    assert not session.round.snapshot().cells
    assert not session.round.snapshot().points
    session.reset()
    assert not session.round.snapshot().last_cells
    assert not session.round.snapshot().last_points
    repo.close()


def test_bad_parent_is_atomic(catalog):
    state = GeoRound(catalog)
    before = state.snapshot()
    wrong = replace(evidence(), cells=(locate_cell(47, 11, "AT", "DE-BY", 0.5),))
    with pytest.raises(ValueError):
        state.accept(state.id, frame(), wrong, 1)
    assert state.snapshot() == before
    wrong = replace(evidence(), points=(LocalPoint("bad", 47, 11, "AT", "DE-BY", 0.5),))
    with pytest.raises(ValueError):
        state.accept(state.id, frame(), wrong, 1)
    assert state.snapshot() == before
