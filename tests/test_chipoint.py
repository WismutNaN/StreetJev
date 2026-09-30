import base64
import json

import httpx
import pytest
from conftest import frame

from streetjev.infrastructure.catalog import load_geography
from streetjev.infrastructure.chipoint import ChipointExpert, GeographicLocator
from streetjev.infrastructure.imajev import InferenceError


@pytest.fixture(scope="module")
def geography():
    return load_geography()


def response(candidates):
    return {
        "schema": "streetjev.chipoint.v1",
        "model": "test",
        "mode": "street_retrieval",
        "candidates": candidates,
    }


def test_local_coordinates_country_region_and_unknown_mass(geography):
    catalog, data = geography
    result = ChipointExpert(catalog, data).convert(
        response(
            [
                {"id": "1", "lat": 48.2082, "lon": 16.3738, "score": 1},
                {"id": "2", "lat": 52.52, "lon": 13.405, "score": 1},
                {"id": "3", "lat": 0, "lon": -140, "score": 1},
            ]
        ),
        10,
    )
    assert result.countries.probabilities["AT"] == pytest.approx(0.5)
    assert result.countries.probabilities["DE"] == pytest.approx(0.5)
    assert result.countries.unknown == pytest.approx(1 / 3)
    assert "AT" in result.regions and "DE" in result.regions
    assert len(json.loads(result.raw_json)["candidates"]) == 3
    assert [(p.latitude, p.longitude) for p in result.points] == [(48.2082, 16.3738), (52.52, 13.405)]
    assert sum(p.mass for p in result.points) == pytest.approx(2 / 3)


def test_no_land_candidate_is_unknown_not_confident_country(geography):
    catalog, data = geography
    result = ChipointExpert(catalog, data).convert(
        response(
            [
                {"id": "1", "lat": 0, "lon": -140, "score": 10},
            ]
        ),
        0,
    )
    assert result.countries.unknown == 1
    assert not result.regions


@pytest.mark.parametrize(
    "candidate",
    [
        {"id": "x", "lat": 91, "lon": 0, "score": 1},
        {"id": "x", "lat": 0, "lon": 181, "score": 1},
        {"id": "x", "lat": 0, "lon": 0, "score": float("nan")},
    ],
)
def test_invalid_candidates_rejected(geography, candidate):
    with pytest.raises(ValueError):
        ChipointExpert(*geography).convert(response([candidate]), 0)


def test_http_contract_and_timeout(geography):
    def handler(request):
        assert request.url.path == "/v1/geolocate"
        assert base64.b64decode(json.loads(request.read())["image"]) == b"jpeg"
        return httpx.Response(200, json=response([{"id": "1", "lat": 48.2082, "lon": 16.3738, "score": 1}]))

    result = ChipointExpert(*geography, transport=httpx.MockTransport(handler)).infer(frame())
    assert result.countries.probabilities["AT"] == pytest.approx(1)

    def timeout(request):
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(InferenceError, match="too slow"):
        ChipointExpert(*geography, transport=httpx.MockTransport(timeout)).infer(frame())


def test_region_border_and_no_invented_nearest_country(geography):
    locator = GeographicLocator(geography[1])
    assert locator.locate(0, -140) == (None, None)
    country, region = locator.locate(47.2692, 11.4041)
    assert country == "AT"
    assert region in geography[0].regions
