import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from conftest import frame

from streetjev.domain.clues import PlaceClues
from streetjev.domain.evidence import Distribution, GeoEvidence
from streetjev.infrastructure.imajev import ImajevExpert, InferenceError


def request_json(request):
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.read()
    )
    parts = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
    assert parts["image"].get_payload(decode=True) == b"jpeg"
    return json.loads(parts["request"].get_payload(decode=True))


def test_official_multipart_contract_and_beam(catalog):
    requests = []

    def handler(request):
        assert request.url.path == "/v1/systemone"
        payload = request_json(request)
        requests.append(payload)
        questions = payload["questions"]
        if "country" in questions:
            return httpx.Response(
                200,
                json={
                    "model": "imajev-4b",
                    "answers": {
                        "country": {
                            "type": "choice",
                            "probabilities": {"AT": 0.8, "DE": 0.2},
                            "unknown_probability": 0.1,
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "answers": {
                    key: {
                        "type": "choice",
                        "probabilities": {code: 1 / len(q["criteria"]) for code in q["criteria"]},
                        "unknown_probability": 0.3,
                    }
                    for key, q in questions.items()
                }
            },
        )

    expert = ImajevExpert(catalog, beam=2, transport=httpx.MockTransport(handler))
    result = expert.infer(frame())
    assert len(requests) == 2
    assert set(requests[1]["questions"]) == {"region_AT", "region_DE"}
    assert result.countries.probabilities["AT"] == pytest.approx(0.8)
    assert result.countries.unknown == 0.1
    assert set(result.regions) == {"AT", "DE"}
    assert json.loads(result.raw_json)["country"]["model"] == "imajev-4b"


def test_high_unknown_skips_regions(catalog):
    def handler(request):
        assert "country" in request_json(request)["questions"]
        return httpx.Response(
            200,
            json={
                "answers": {
                    "country": {
                        "type": "choice",
                        "probabilities": {"AT": 0.5, "DE": 0.5},
                        "unknown_probability": 0.99,
                    }
                }
            },
        )

    result = ImajevExpert(catalog, transport=httpx.MockTransport(handler)).infer(frame())
    assert not result.regions


def test_partial_distribution_is_not_silently_normalized(catalog):
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "answers": {"country": {"type": "choice", "probabilities": {"AT": 1}, "unknown_probability": 0}}
            },
        )
    )
    with pytest.raises(InferenceError, match="набор вариантов"):
        ImajevExpert(catalog, transport=transport).infer(frame())


def test_connection_failure_has_actionable_message(catalog):
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(InferenceError, match="Запустите сервис"):
        ImajevExpert(catalog, transport=httpx.MockTransport(handler)).infer(frame())


def test_chipoint_opens_missing_country_without_repeating_initial_requests(catalog):
    calls = []

    def handler(request):
        questions = request_json(request)["questions"]
        calls.append(questions)
        assert len(questions) <= 8
        values = {
            "country": {"AT": 1, "DE": 0},
            "driving_side": {"left": 0, "right": 0, "unclear": 1},
            "hemisphere": {"north": 0, "south": 0, "unclear": 1},
        }
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {
                    key: {
                        "type": "choice",
                        "probabilities": values.get(key, dict.fromkeys(q["criteria"], 1)),
                        "unknown_probability": 0,
                    }
                    for key, q in questions.items()
                },
            },
        )

    expert = ImajevExpert(catalog, beam=1, transport=httpx.MockTransport(handler))
    primary = expert.infer(frame())
    secondary = GeoEvidence("chipoint", "test", Distribution({"AT": 0, "DE": 1}), {}, "{}", 0)
    assert set(primary.regions) == {"AT"}
    assert set(calls[0]) == {"country", "hemisphere", "driving_side"}
    refined = expert.refine(frame(), primary, secondary)
    assert set(refined.regions) == {"AT", "DE"}
    assert set(calls[2]) == {"region_DE"}
    assert refined.countries == primary.countries
    assert json.loads(refined.raw_json)["country"] == json.loads(primary.raw_json)["country"]
    assert expert.refine(frame(), refined, secondary) is refined
    assert len(calls) == 3
    expert.beam = 0
    assert not expert.missing_regions(primary, secondary)


def test_side_of_traffic_can_change_branch_selection(catalog):
    calls = []

    def handler(request):
        questions = request_json(request)["questions"]
        calls.append(questions)
        values = {
            "country": {"AT": 0.55, "DE": 0.45},
            "driving_side": {"left": 1, "right": 0, "unclear": 0},
            "hemisphere": {"north": 0, "south": 0, "unclear": 1},
        }
        return httpx.Response(
            200,
            json={
                "answers": {
                    key: {
                        "type": "choice",
                        "probabilities": values.get(key, dict.fromkeys(q["criteria"], 1)),
                        "unknown_probability": 0,
                    }
                    for key, q in questions.items()
                }
            },
        )

    # Синтетические стороны движения проверяют алгоритм, не справочник реальных стран.
    facts = {"AT": PlaceClues("right"), "DE": PlaceClues("left")}
    result = ImajevExpert(catalog, beam=1, transport=httpx.MockTransport(handler), facts=facts).infer(frame())
    assert result.countries.probabilities["DE"] > result.countries.probabilities["AT"]
    assert set(calls[1]) == {"region_DE"}
    assert result.countries.unknown > 0


def test_profile_matches_raw_decode_for_initial_and_refined_regions(catalog):
    from streetjev.domain.calibration import CalibrationParameters, CalibrationProfile, adjust_imajev

    def handler(request):
        questions = request_json(request)["questions"]
        values = {
            "country": {"AT": 0.99, "DE": 0.01},
            "driving_side": {"left": 0, "right": 0.9, "unclear": 0.1},
            "hemisphere": {"north": 0.9, "south": 0.05, "unclear": 0.05},
            "region_AT": {"AT-7": 0.97, "AT-5": 0.03},
            "region_DE": {"DE-BY": 0.99, "DE-HE": 0.01},
        }
        return httpx.Response(
            200,
            json={
                "model": "test",
                "answers": {
                    key: {"type": "choice", "probabilities": values[key], "unknown_probability": 0.1}
                    for key in questions
                },
            },
        )

    params = CalibrationParameters(imajev_country_temperature=2, imajev_region_temperature=4, driving_strength=0.1)
    profile = CalibrationProfile(params, ("test",))
    facts = {"AT": PlaceClues("left", "north"), "DE-BY": PlaceClues("left", "south")}
    expert = ImajevExpert(catalog, beam=1, transport=httpx.MockTransport(handler), facts=facts, profile=profile)
    initial = expert.infer(frame())
    decoded = adjust_imajev(expert.decode(initial.raw_json), params, facts)
    assert initial.countries == decoded.countries
    assert initial.regions == decoded.regions
    secondary = GeoEvidence("chipoint", "test", Distribution({"AT": 0, "DE": 1}), {}, "{}", 0)
    refined = expert.refine(frame(), initial, secondary)
    decoded = adjust_imajev(expert.decode(refined.raw_json), params, facts)
    assert refined.countries == initial.countries == decoded.countries
    assert refined.regions == decoded.regions
    assert refined.regions["AT"] == initial.regions["AT"]
