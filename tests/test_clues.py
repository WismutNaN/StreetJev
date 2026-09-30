from dataclasses import replace

import pytest
from conftest import evidence

from streetjev.domain.clues import PlaceClues, apply_clues
from streetjev.domain.evidence import Distribution
from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.clues import load_clue_facts


def test_unclear_and_unknown_cues_are_neutral():
    facts = {"AT": PlaceClues("right", "north"), "DE": PlaceClues("left", "south")}
    for probabilities, unknown in (
        ({"left": 0, "right": 0, "unclear": 1}, 0),
        ({"left": 1, "right": 0, "unclear": 0}, 1),
        ({"left": 0.5, "right": 0.5, "unclear": 0}, 0),
    ):
        result = apply_clues(evidence(), {"driving_side": Distribution(probabilities, unknown)}, facts)
        assert result.countries == evidence().countries


def test_contradictions_reduce_support_but_never_exclude_country():
    clues = {
        "driving_side": Distribution({"left": 1, "right": 0, "unclear": 0}),
        "hemisphere": Distribution({"north": 0, "south": 1, "unclear": 0}),
    }
    facts = {"AT": PlaceClues("right", "north"), "DE": PlaceClues("left", "south")}
    result = apply_clues(evidence(), clues, facts)
    assert 0 < result.countries.probabilities["AT"] < 0.8
    assert result.countries.unknown > 0
    # Ни одной стране не добавлена абсолютная уверенность за совпадение признака.
    for code, probability in result.countries.probabilities.items():
        assert probability * (1 - result.countries.unknown) <= evidence().countries.probabilities[code] + 1e-12


def test_missing_facts_and_equator_are_neutral_and_region_scores_stay_conditional():
    original = replace(evidence(), regions={"AT": Distribution({"AT-7": 0.5, "AT-5": 0.5})})
    clues = {"hemisphere": Distribution({"north": 1, "south": 0, "unclear": 0})}
    facts = {"AT": PlaceClues(), "AT-7": PlaceClues(hemisphere="south"), "AT-5": PlaceClues(hemisphere="north")}
    result = apply_clues(original, clues, facts)
    assert result.countries == original.countries
    assert result.regions["AT"].probabilities["AT-5"] > 0.5
    assert sum(result.regions["AT"].probabilities.values()) == pytest.approx(1)


def test_bundled_guess_map_facts_and_equatorial_geography():
    _, geography = load_geography()
    facts = load_clue_facts(geography)
    assert facts["GB"].driving_side == "left"
    assert facts["DE"].driving_side == "right"
    assert facts["AU"].hemisphere == "south"
    assert facts["DE"].hemisphere == "north"
    assert facts["BR"].hemisphere is None
    assert facts["ID"].hemisphere is None
