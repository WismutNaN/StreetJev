import math

import pytest
from conftest import evidence, frame

from streetjev.domain.evidence import Distribution
from streetjev.domain.round import GeoRound, pool_scores


def test_repeated_opinions_do_not_manufacture_certainty(catalog):
    state = GeoRound(catalog)
    state.accept(state.id, frame(), evidence(), 1)
    state.accept(state.id, frame(255, "second"), evidence(), 1)
    assert state.countries["AT"] == pytest.approx(0.8)
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


def test_long_sequence_stays_bounded_and_can_reverse():
    scores, weight = {"AT": 0.5, "DE": 0.5}, 0.0
    for _ in range(1000):
        scores = pool_scores(scores, weight, Distribution({"AT": 0.8, "DE": 0.2}), 0.5)
        weight += 0.5
    assert scores["AT"] == pytest.approx(0.8)
    for _ in range(1100):
        scores = pool_scores(scores, weight, Distribution({"AT": 0.2, "DE": 0.8}), 0.5)
        weight += 0.5
    assert all(math.isfinite(p) for p in scores.values())
    assert sum(scores.values()) == pytest.approx(1)
    assert 0.5 < scores["DE"] < 0.8


def test_conflicting_frames_remain_ambiguous(catalog):
    state = GeoRound(catalog)
    state.accept(state.id, frame(), evidence(at=0.9), 1)
    state.accept(state.id, frame(255, "second"), evidence(at=0.1), 1)
    snapshot = state.snapshot()
    assert snapshot.countries["AT"] == pytest.approx(0.5)
    assert snapshot.last_countries["AT"] == pytest.approx(0.1)
    assert snapshot.known_frames == 2
    assert snapshot.agreeing_frames == 1


def test_weight_uses_novelty_and_unknown(catalog):
    state = GeoRound(catalog)
    state.accept(state.id, frame(), evidence(at=0.8), 1)
    state.accept(state.id, frame(255, "second"), evidence(at=0.1, unknown=0.5), 0.5)
    assert state.effective_frames == 1.25
    assert state.countries["AT"] == pytest.approx((0.8 + 0.25 * 0.1) / 1.25)


def test_unknown_after_valid_frame_does_not_erase_knowledge(catalog):
    state = GeoRound(catalog)
    state.accept(state.id, frame(), evidence(), 1)
    state.accept(state.id, frame(255, "unknown"), evidence(at=0, unknown=1), 1)
    snapshot = state.snapshot()
    assert snapshot.countries["AT"] == pytest.approx(0.8)
    assert snapshot.known_frames == 1
    assert snapshot.last_unknown == 1


def test_regions_are_pooled_without_promoting_country(catalog):
    state = GeoRound(catalog)
    first = evidence(at=0.4, regions={"AT": Distribution({"AT-7": 0.9, "AT-5": 0.1})})
    second = evidence(at=0.8, regions={"AT": Distribution({"AT-7": 0.3, "AT-5": 0.7})})
    state.accept(state.id, frame(), first, 1)
    state.accept(state.id, frame(255, "second"), second, 1)
    snapshot = state.snapshot()
    assert snapshot.countries["AT"] == pytest.approx(0.6)
    conditional = (0.4 * 0.9 + 0.8 * 0.3) / 1.2
    assert snapshot.regions["AT-7"] == pytest.approx(0.6 * conditional)
    assert snapshot.last_regions["AT-7"] == pytest.approx(0.8 * 0.3)


def test_pooling_is_order_independent(catalog):
    rows = [(0.8, 0.2, 1), (0.3, 0.0, 0.5), (0.5, 0.6, 0.8)]

    def combined(rows):
        state = GeoRound(catalog)
        for i, (score, unknown, weight) in enumerate(rows):
            state.accept(state.id, frame(i, str(i)), evidence(at=score, unknown=unknown), weight)
        return state.countries

    assert combined(rows) == pytest.approx(combined(list(reversed(rows))))


def test_snapshot_copies_are_detached_from_aggregate(catalog):
    state = GeoRound(catalog)
    state.accept(state.id, frame(), evidence(), 1)
    snapshot = state.snapshot()
    snapshot.countries["AT"] = 0
    snapshot.last_countries["AT"] = 0
    assert state.snapshot().countries["AT"] == pytest.approx(0.8)
    assert state.snapshot().last_countries["AT"] == pytest.approx(0.8)


def test_distribution_is_immutable():
    original = {"AT": 0.8, "DE": 0.2}
    d = Distribution(original)
    original["AT"] = 0
    assert d.probabilities["AT"] == 0.8
    with pytest.raises(TypeError):
        d.probabilities["AT"] = 0
