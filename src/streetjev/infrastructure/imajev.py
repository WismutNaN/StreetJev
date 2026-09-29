import json
import time

import httpx

from streetjev.domain.evidence import Distribution, Frame, GeoEvidence
from streetjev.domain.geography import GeoCatalog


class InferenceError(RuntimeError):
    pass


class ImajevExpert:
    """Anti-corruption layer официального API /v1/systemone."""

    def __init__(
        self,
        catalog: GeoCatalog,
        base_url: str = "http://127.0.0.1:18765",
        beam: int = 3,
        timeout: float = 90,
        transport=None,
    ):
        url = httpx.URL(base_url)
        if url.scheme not in ("http", "https") or not url.host:
            raise ValueError("Нужен адрес http:// или https:// сервера imajev")
        if not 0 <= beam <= 5:
            raise ValueError("Число ветвей должно быть от 0 до 5")
        if not 2 <= len(catalog.countries) <= 254:
            raise ValueError("imajev поддерживает 2–254 варианта в одном вопросе")
        self.catalog, self.beam = catalog, beam
        self.endpoint = base_url.rstrip("/") + "/v1/systemone"
        self.timeout, self.transport = timeout, transport

    @staticmethod
    def _distribution(answer: dict, keys: set[str]) -> Distribution:
        if answer.get("type") != "choice":
            raise ValueError("Сервер вернул ответ другого типа")
        values = answer["probabilities"]
        if set(values) != keys:
            raise ValueError("Сервер вернул неполный или неизвестный набор вариантов")
        unknown = float(answer["unknown_probability"])
        # probabilities официального API условны по известной массе; unknown отдельный.
        return Distribution({key: float(value) for key, value in values.items()}, unknown)

    def _request(self, client: httpx.Client, frame: Frame, questions: dict) -> dict:
        payload = {
            "state": "A screenshot of an outdoor location. Use only visible geographic clues.",
            "questions": questions,
        }
        response = client.post(
            self.endpoint,
            data={"request": json.dumps(payload)},
            files={"image": ("frame.jpg", frame.jpeg, "image/jpeg")},
        )
        response.raise_for_status()
        return response.json()

    def infer(self, frame: Frame) -> GeoEvidence:
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport, trust_env=False) as client:
                country_response = self._request(
                    client,
                    frame,
                    {
                        "country": {
                            "type": "choice",
                            "instructions": "Which country or territory was this image most likely captured in?",
                            "criteria": {p.code: p.name for p in self.catalog.countries.values()},
                        }
                    },
                )
                countries = self._distribution(country_response["answers"]["country"], set(self.catalog.countries))
                top = sorted(countries.probabilities, key=countries.probabilities.get, reverse=True)[: self.beam]
                questions, expected = {}, {}
                if countries.unknown < 0.8:
                    for country in top:
                        regions = self.catalog.regions_of(country)
                        if 2 <= len(regions) <= 254:
                            key = "region_" + country
                            questions[key] = {
                                "type": "choice",
                                "instructions": f"Assuming the image is in {self.catalog.countries[country].name}, "
                                "which administrative region is most consistent with the visible clues?",
                                "criteria": {p.code: p.name for p in regions},
                            }
                            expected[key] = country
                regions, region_response = {}, None
                if questions:
                    region_response = self._request(client, frame, questions)
                    for key, country in expected.items():
                        regions[country] = self._distribution(
                            region_response["answers"][key], {p.code for p in self.catalog.regions_of(country)}
                        )
                raw = json.dumps({"country": country_response, "regions": region_response}, ensure_ascii=False)
                return GeoEvidence(
                    "imajev_hierarchical",
                    str(country_response.get("model", "imajev")),
                    countries,
                    regions,
                    raw,
                    (time.perf_counter() - started) * 1000,
                )
        except httpx.ConnectError as exc:
            raise InferenceError(
                "Нет соединения с imajev. Запустите сервис из services/imajev или включите демо."
            ) from exc
        except httpx.TimeoutException as exc:
            raise InferenceError("imajev не ответил за отведённое время. Проверьте журнал сервиса.") from exc
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise InferenceError(f"Ошибка сервиса imajev: HTTP {exc.response.status_code}. {detail}") from exc
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise InferenceError(f"Некорректный ответ imajev: {exc}") from exc
