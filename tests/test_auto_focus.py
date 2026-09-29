from streetjev.presentation.auto_focus import Bounds, focus_bounds

BOXES = {
    "AT": Bounds(9, -65, 17, -58),
    "DE": Bounds(6, -74, 15, -59),
    "US": Bounds(-125, -65, -66, -28),
    "JP": Bounds(129, -52, 146, -30),
}


def test_neighboring_leaders_zoom_with_regional_context():
    result = focus_bounds({"AT": 0.6, "DE": 0.35, "JP": 0.05}, BOXES)
    assert result is not None
    assert result.left < 6 and result.right > 17
    assert result.right - result.left >= 30


def test_distant_competitor_prevents_zoom():
    assert focus_bounds({"AT": 0.6, "JP": 0.4}, BOXES) is None
    assert focus_bounds({"AT": 0.5, "US": 0.5}, BOXES) is None


def test_empty_and_flat_world_do_not_zoom():
    assert focus_bounds({}, BOXES) is None
    assert focus_bounds(dict.fromkeys(BOXES, 0.25), BOXES) is None


def test_antimeridian_spanning_geometry_keeps_world():
    assert focus_bounds({"FJ": 1}, {"FJ": Bounds(-180, 10, 180, 20)}) is None
