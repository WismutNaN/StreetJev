import pytest
from conftest import evidence, frame

from streetjev.domain.evidence import Distribution
from streetjev.domain.round import GeoRound
from streetjev.presentation.map_scale import HeatScale, format_score
from streetjev.presentation.scores import display_scores


def test_small_regional_values_get_contrast_without_changing_numbers():
    values = [0.0001, 0.0003, 0.0008]
    scale = HeatScale.from_scores(values)
    assert scale.intensity(values[0]) == 0
    assert scale.intensity(values[1]) > 0.4
    assert scale.intensity(values[2]) == pytest.approx(1)
    assert scale.value_at(1) == pytest.approx(0.0008)
    assert scale.legend()[-1][1] == "0,080%"
    assert values == [0.0001, 0.0003, 0.0008]


def test_flat_scores_never_create_hotspot():
    scale = HeatScale.from_scores([0.01] * 100)
    assert scale.flat
    assert scale.intensity(0.01) == 0
    assert len(scale.legend()) == 1
    assert "Равные" in scale.legend()[0][1]


def test_absolute_scale_does_not_renormalize():
    scale = HeatScale.from_scores([0.001, 0.003], "absolute")
    assert scale.intensity(0.003) == 0.003
    assert scale.maximum == 1


def test_legend_matches_color_transform():
    scale = HeatScale.from_scores([0.001, 0.2])
    for intensity, _ in scale.legend():
        assert scale.intensity(scale.value_at(intensity)) == pytest.approx(intensity)


def test_empty_and_zero_scores():
    assert HeatScale.from_scores([]).empty
    assert HeatScale.from_scores([0, 0]).flat


@pytest.mark.parametrize("values", [[float("nan")], [float("inf")], [-0.1], [1.1]])
def test_invalid_scale_data_rejected(values):
    with pytest.raises(ValueError):
        HeatScale.from_scores(values)


def test_display_round_does_not_reuse_previous_region_for_latest_frame(catalog):
    state = GeoRound(catalog)
    state.accept(state.id, frame(), evidence(regions={"AT": Distribution({"AT-7": 0.9, "AT-5": 0.1})}), 1)
    state.accept(state.id, frame(255, "second"), evidence(at=0.2), 1)
    assert display_scores(state.snapshot()).observed_regions == frozenset({"AT"})
    assert not display_scores(state.snapshot(), "last").observed_regions


def test_unknown_latest_frame_is_not_a_map(catalog):
    state = GeoRound(catalog)
    state.accept(state.id, frame(), evidence(), 1)
    state.accept(state.id, frame(255, "unknown"), evidence(unknown=1), 1)
    assert display_scores(state.snapshot()).active
    assert not display_scores(state.snapshot(), "last").active


def test_formatting_does_not_round_to_false_certainty_or_zero():
    assert format_score(0.999837) == ">99,9%"
    assert format_score(0.00005) == "0,005%"
    assert format_score(0.000001) == "<0,001%"
