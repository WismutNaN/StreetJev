import math

import pytest
from conftest import evidence, frame

from streetjev.domain.evidence import Distribution
from streetjev.domain.round import GeoRound, update_posterior


def test_independent_observations_accumulate(catalog):
    state = GeoRound(catalog)
    state.accept(state.id, frame(), evidence(), 1)
    state.accept(state.id, frame(255, "second"), evidence(), 1)
    assert state.countries["AT"] == pytest.approx(16 / 17)
    assert math.fsum(state.countries.values()) == pytest.approx(1)


def test_duplicate_and_near_duplicate_have_no_weight(catalog):
    state = GeoRound(catalog)
    first = frame()
    state.accept(state.id, first, evidence(), state.novelty(first))
    assert state.novelty(frame(0, "duplicate")) == 0
    assert state.novelty(frame(2, "almost")) == 0
    assert state.novelty(frame(255, "new-angle")) == 1
    assert not state.accept(state.id, first, evidence(), 1)
    assert state.frames == 1


def test_unknown_does_not_change_posterior(catalog):
    state = GeoRound(catalog)
    state.accept(state.id, frame(), evidence(at=1, unknown=1), 1)
    assert state.countries == {"AT": 0.5, "DE": 0.5}
    assert state.effective_frames == 0


def test_old_round_rejected(catalog):
    previous, current = GeoRound(catalog), GeoRound(catalog)
    assert not current.accept(previous.id, frame(), evidence(), 1)
    assert current.frames == 0


def test_region_mass_never_exceeds_country(catalog):
    state = GeoRound(catalog)
    state.accept(state.id, frame(), evidence(regions={"AT": Distribution({"AT-7": 0.9, "AT-5": 0.1})}), 1)
    snap = state.snapshot()
    assert snap.regions["AT-7"] > snap.regions["AT-5"]
    assert snap.regions["AT-7"] + snap.regions["AT-5"] == pytest.approx(snap.countries["AT"])
    assert snap.observed_regions == frozenset({"AT"})


def test_invalid_evidence_cannot_partially_change_round(catalog):
    state = GeoRound(catalog)
    before = state.snapshot()
    with pytest.raises(ValueError):
        state.accept(state.id, frame(), evidence(regions={"AT": Distribution({"unknown": 1})}), 1)
    assert state.snapshot() == before


@pytest.mark.parametrize(
    "probabilities,unknown", [({"AT": float("nan")}, 0), ({"AT": -1}, 0), ({"AT": 0}, 0), ({"AT": 1}, 2)]
)
def test_invalid_probabilities_rejected(probabilities, unknown):
    with pytest.raises(ValueError):
        Distribution(probabilities, unknown)


def test_long_sequence_stays_finite_and_can_reverse():
    posterior = {"AT": 0.5, "DE": 0.5}
    for _ in range(1000):
        posterior = update_posterior(posterior, Distribution({"AT": 1, "DE": 0}), 0.5)
    for _ in range(1100):
        posterior = update_posterior(posterior, Distribution({"AT": 0, "DE": 1}), 0.5)
    assert all(math.isfinite(p) for p in posterior.values())
    assert sum(posterior.values()) == pytest.approx(1)
    assert posterior["DE"] > 0.99


def test_distribution_is_immutable():
    original = {"AT": 0.8, "DE": 0.2}
    d = Distribution(original)
    original["AT"] = 0
    assert d.probabilities["AT"] == 0.8
    with pytest.raises(TypeError):
        d.probabilities["AT"] = 0
