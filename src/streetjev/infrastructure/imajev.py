import json
import time
from dataclasses import replace

import httpx

from streetjev.domain.calibration import CalibrationProfile, adjust_imajev
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
        facts=None,
        profile=None,
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
        self.facts = facts or {}
        self.profile = profile or CalibrationProfile()

    def decode(self, raw_json, elapsed_ms=0):
        """Восстанавливает исходные оценки API, без повторных запросов и поправок."""
        raw = json.loads(raw_json)
        response = raw["country"]
        countries = self._distribution(response["answers"]["country"], set(self.catalog.countries))
        clues = {}
        for key, choices in (
            ("driving_side", {"left", "right", "unclear"}),
            ("hemisphere", {"north", "south", "unclear"}),
        ):
            if key in response["answers"]:
                clues[key] = self._distribution(response["answers"][key], choices)
        regions = {}
        for branch in (raw.get("regions"), raw.get("chipoint_region_refinement")):
            for key, value in (branch or {}).get("answers", {}).items():
                code = key.removeprefix("region_")
                if not key.startswith("region_") or code not in self.catalog.countries:
                    raise ValueError("Неизвестная региональная ветвь")
                regions[code] = self._distribution(value, {r.code for r in self.catalog.regions_of(code)})
        return GeoEvidence(
            "imajev_hierarchical",
            str(response.get("model", "imajev")),
            countries,
            regions,
            raw_json,
            elapsed_ms,
            clues,
        )

    def region_candidates(self, evidence):
        if evidence.countries.unknown >= 0.8:
            return []
        scores = evidence.countries.probabilities
        return [code for code in sorted(scores, key=scores.get, reverse=True) if scores[code] > 0][: self.beam]

    def missing_regions(self, primary, secondary):
        return [
            code
            for code in self.region_candidates(secondary)
            if code not in primary.regions and 2 <= len(self.catalog.regions_of(code)) <= 254
        ]

    def _regions(self, client, frame, codes):
        questions = {}
        for country in codes:
            regions = self.catalog.regions_of(country)
            if 2 <= len(regions) <= 254:
                questions["region_" + country] = {
                    "type": "choice",
                    "instructions": f"Assuming the image is in {self.catalog.countries[country].name}, "
                    "which administrative region is most consistent with the visible clues?",
                    "criteria": {p.code: p.name for p in regions},
                }
        if not questions:
            return {}, None
        response = self._request(client, frame, questions)
        return {
            key.removeprefix("region_"): self._distribution(response["answers"][key], set(q["criteria"]))
            for key, q in questions.items()
        }, response

    def refine(self, frame, primary, secondary):
        """Только недостающие ветви Chipoint. Повторно страну и признаки не спрашиваем."""
        codes = self.missing_regions(primary, secondary)
        if not codes:
            return primary
        started = time.perf_counter()
        with httpx.Client(timeout=self.timeout, transport=self.transport, trust_env=False) as client:
            regions, response = self._regions(client, frame, codes)
        extra = replace(primary, regions=regions)
        # Страны уже скорректированы; здесь уточняются только новые региональные ветви.
        adjusted = adjust_imajev(extra, self.profile.for_model("imajev", primary.model_version), self.facts)
        raw = json.loads(primary.raw_json)
        raw["chipoint_region_refinement"] = response
        return replace(
            primary,
            regions={**primary.regions, **adjusted.regions},
            raw_json=json.dumps(raw, ensure_ascii=False),
            elapsed_ms=primary.elapsed_ms + (time.perf_counter() - started) * 1000,
        )

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
                        },
                        "driving_side": {
                            "type": "choice",
                            "instructions": "Which side of a two-way road does traffic keep to? Use visible moving "
                            "traffic and road layout only. Do not infer from a guessed country, the camera's lane, "
                            "a single parked car or a one-way road. Choose unclear if not directly supported.",
                            "criteria": {
                                "left": "Left-hand traffic",
                                "right": "Right-hand traffic",
                                "unclear": "Not visible or ambiguous",
                            },
                        },
                        "hemisphere": {
                            "type": "choice",
                            "instructions": "Is there direct visible evidence for north or south of the equator? "
                            "Do not infer from the country guess, driving side or generic vegetation. The sun or "
                            "shadows without a readable compass/north orientation are insufficient. Season, time "
                            "of day and tropical locations make solar clues ambiguous. Choose unclear unless "
                            "the visible evidence distinguishes the hemispheres.",
                            "criteria": {
                                "north": "Northern hemisphere",
                                "south": "Southern hemisphere",
                                "unclear": "Insufficient or ambiguous evidence",
                            },
                        },
                    },
                )
                countries = self._distribution(country_response["answers"]["country"], set(self.catalog.countries))
                clues = {}
                for key, choices in (
                    ("driving_side", {"left", "right", "unclear"}),
                    ("hemisphere", {"north", "south", "unclear"}),
                ):
                    if key in country_response["answers"]:
                        clues[key] = self._distribution(country_response["answers"][key], choices)
                base = GeoEvidence(
                    "imajev_hierarchical",
                    str(country_response.get("model", "imajev")),
                    countries,
                    {},
                    "{}",
                    0,
                    clues,
                )
                parameters = self.profile.for_model("imajev", base.model_version)
                adjusted = adjust_imajev(base, parameters, self.facts)
                regions, region_response = self._regions(client, frame, self.region_candidates(adjusted))
                raw = json.dumps({"country": country_response, "regions": region_response}, ensure_ascii=False)
                return adjust_imajev(
                    replace(
                        base, regions=regions, raw_json=raw, elapsed_ms=(time.perf_counter() - started) * 1000
                    ),
                    parameters,
                    self.facts,
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
