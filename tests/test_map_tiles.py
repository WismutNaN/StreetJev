import math

from streetjev.infrastructure.map_tiles import visible_tiles


def test_visible_tiles_scale_limits_and_clipping():
    assert visible_tiles((-180, -180, 180, 180), 0.7) == [(0, 0, 0)]
    assert visible_tiles((200, 200, 210, 210), 100) == []
    for scale in (3, 20, 700, 10000, 400000):
        bounds = (11, -52, 11 + 1000 / scale, -52 + 700 / scale)
        keys = visible_tiles(bounds, scale)
        assert 1 <= len(keys) <= 80
        for zoom, x, y in keys:
            assert 0 <= zoom <= 14
            assert 0 <= x < 2**zoom and 0 <= y < 2**zoom
        assert keys[0][0] == min(14, math.ceil(math.log2(360 * scale / 512)))


def test_no_prefetch_or_wrapping_beyond_date_line():
    keys = visible_tiles((179.99, -0.01, 181, 0.01), 2000)
    for z, x, y in keys:
        size = 360 / 2**z
        assert -180 + (x + 1) * size >= 179.99
        assert -180 + x * size <= 180
        assert -180 + y * size <= 0.01 <= -180 + (y + 1) * size or -180 + (y + 1) * size >= -0.01


def test_wide_high_dpi_view_is_bounded_without_missing_edges():
    keys = visible_tiles((-180, -180, 180, 180), 400000)
    assert len(keys) <= 96
    zoom = keys[0][0]
    assert (zoom, 0, 0) in keys
    assert (zoom, 2**zoom - 1, 2**zoom - 1) in keys
