import io
import json

import httpx
import pytest
from PIL import Image

from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.chipoint import ChipointExpert
from streetjev.infrastructure.coverage import CoverageDensity


def png(color, size=(256, 256)):
    stream = io.BytesIO()
    Image.new("RGBA", size, color).save(stream, format="PNG")
    return stream.getvalue()


def test_density_recognizes_empty_and_blue_tiles():
    assert CoverageDensity.density(png((0, 0, 0, 0), (1, 1))) == 0
    assert CoverageDensity.density(png((14, 74, 231, 129))) == 1
    with pytest.raises(ValueError):
        CoverageDensity.density(png((0, 0, 0, 0), (50, 50)))
    with pytest.raises(ValueError):
        CoverageDensity.density(png((255, 255, 255, 255)))


def test_cache_and_request_cap_with_failure_neutrality():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        raise httpx.ReadTimeout("offline", request=request)

    coverage = CoverageDensity(httpx.MockTransport(handler))
    points = [{"lat": 40, "lon": i * 2} for i in range(20)]
    samples = coverage.sample(points)
    assert len(calls) == 8
    assert all(coverage.factor(value) == 1 for value in samples.values())
    assert coverage.sample(points) == samples
    assert len(calls) == 8
    assert coverage.tile(90, 180) == (0, 0)


def test_density_is_a_small_penalty_not_a_country_filter():
    catalog, geography = load_geography()
    coverage = CoverageDensity(
        httpx.MockTransport(lambda _: httpx.Response(200, content=png((0, 0, 0, 0), (1, 1))))
    )
    payload = {
        "schema": "streetjev.chipoint.v1",
        "mode": "street_retrieval",
        "model": "test",
        "candidates": [{"id": "one", "lat": 48.2082, "lon": 16.3738, "score": 1}],
    }
    result = ChipointExpert(catalog, geography, coverage=coverage).convert(payload, 0)
    assert result.countries.probabilities["AT"] == 1
    assert result.countries.unknown == pytest.approx(0.15)
    assert json.loads(result.raw_json)["coverage"]["tiles"][0]["density"] == 0
