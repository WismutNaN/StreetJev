import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from conftest import frame

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
