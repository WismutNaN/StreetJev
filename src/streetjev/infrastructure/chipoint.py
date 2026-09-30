"""HTTP и координаты Chipoint -> единый контракт evidence. Геометрия за пределами домена."""

import base64
import json
import math
import time

import httpx
from shapely import Point, STRtree
from shapely.geometry import shape

from streetjev.domain.calibration import CalibrationProfile
from streetjev.domain.evidence import Distribution, GeoEvidence
from streetjev.domain.localization import combine_cells, locate_cell
from streetjev.infrastructure.coverage import CoverageDensity
from streetjev.infrastructure.imajev import InferenceError


class GeographicLocator:
    def __init__(self, geography):
        self.places, geometries = [], []
        for feature in geography["features"]:
            geometry = shape(feature["geometry"])
            if geometry.is_empty:
                continue
            self.places.append(feature["properties"])
            geometries.append(geometry)
        self.tree = STRtree(geometries)
        self._locations = {}

    def locate(self, lat, lon):
        key = (lat, lon)
        if key in self._locations:
            return self._locations[key]
        matches = [self.places[int(i)] for i in self.tree.query(Point(lon, lat), predicate="intersects")]
        regions = sorted((p for p in matches if p.get("country")), key=lambda p: p["code"])
        if regions:
            result = regions[0]["country"], regions[0]["code"]
        else:
            countries = sorted((p for p in matches if not p.get("country")), key=lambda p: p["code"])
            result = (countries[0]["code"], None) if countries else (None, None)
        if len(self._locations) >= 32768:
            self._locations.clear()
        self._locations[key] = result
        return result


class ChipointExpert:
    def __init__(
        self,
        catalog,
        geography,
        base_url="http://127.0.0.1:18766",
        timeout=20,
        transport=None,
        coverage=None,
        profile=None,
    ):
        url = httpx.URL(base_url)
        if url.scheme not in ("http", "https") or not url.host:
            raise ValueError("Нужен HTTP-адрес сервиса Chipoint")
        self.catalog, self.geography = catalog, geography
        self.endpoint = base_url.rstrip("/") + "/v1/geolocate"
        self.timeout, self.transport = timeout, transport
        self.coverage = coverage
        self.profile = profile or CalibrationProfile()
        self.locator = GeographicLocator(geography)

    def convert(self, payload, elapsed_ms):
        if payload.get("schema") != "streetjev.chipoint.v1" or payload.get("mode") != "street_retrieval":
            raise ValueError("Несовместимый контракт Chipoint")
        candidates = payload["candidates"]
        if not 1 <= len(candidates) <= 200:
            raise ValueError("Неверное количество кандидатов Chipoint")
        ids, scores = set(), []
        for candidate in candidates:
            lat, lon, score = (float(candidate[key]) for key in ("lat", "lon", "score"))
            if (
                not all(math.isfinite(v) for v in (lat, lon, score))
                or not -90 <= lat <= 90
                or not -180 <= lon <= 180
            ):
                raise ValueError("Некорректный кандидат Chipoint")
            if candidate["id"] in ids:
                raise ValueError("Повторный кандидат Chipoint")
            ids.add(candidate["id"])
            scores.append(score)
        # Температура 2 — консервативная эвристика для z-scores, не географическая калибровка.
        parameters = self.profile.for_model("chipoint", str(payload["model"]))
        weights = [math.exp((score - max(scores)) / parameters.chipoint_temperature) for score in scores]
        total = sum(weights)
        locations = [self.locator.locate(float(c["lat"]), float(c["lon"])) for c in candidates]
        densities = {}
        saved = payload.get("coverage", {})
        if saved.get("source") == "google_svv" and saved.get("zoom") == CoverageDensity.zoom:
            for tile in saved.get("tiles", []):
                density = tile["density"]
                if density is not None and (not math.isfinite(density) or not 0 <= density <= 1):
                    raise ValueError("Некорректная плотность покрытия")
                densities[tile["x"], tile["y"]] = density
        if self.coverage:
            # Сначала по одному сильному совпадению каждой страны, затем остальные точки.
            representatives = {}
            for i in sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True):
                country = locations[i][0]
                if country:
                    representatives.setdefault(country, candidates[i])
            densities = self.coverage.sample(list(representatives.values()))
            payload = {
                **payload,
                "coverage": {
                    "source": "google_svv",
                    "zoom": self.coverage.zoom,
                    "max_penalty": parameters.coverage_penalty,
                    "tiles": [{"x": x, "y": y, "density": density} for (x, y), density in densities.items()],
                },
            }
        countries = dict.fromkeys(self.catalog.countries, 0.0)
        regions, located_by_country, missing = {}, {}, 0.0
        cells = []
        for candidate, weight, (country, region) in zip(candidates, weights, locations, strict=True):
            mass = weight / total
            if densities:
                density = densities.get(CoverageDensity.tile(candidate["lat"], candidate["lon"]))
                factor = CoverageDensity.factor(density, parameters.coverage_penalty)
                missing += mass * (1 - factor)
                mass *= factor
            if country not in countries:
                missing += mass
                continue
            countries[country] += mass
            if mass > 0:
                cells.append(locate_cell(float(candidate["lat"]), float(candidate["lon"]), country, region, mass))
            if region and region in self.catalog.regions:
                regions.setdefault(
                    country, dict.fromkeys((r.code for r in self.catalog.regions_of(country)), 0.0)
                )[region] += mass
                located_by_country[country] = located_by_country.get(country, 0) + mass
        known = sum(countries.values())
        return GeoEvidence(
            "chipoint",
            str(payload["model"]),
            Distribution(countries, unknown=min(1, missing) if known else 1),
            {
                code: Distribution(values, unknown=max(0, 1 - located_by_country[code] / countries[code]))
                for code, values in regions.items()
            },
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
            elapsed_ms,
            cells=combine_cells(cells),
        )

    def infer(self, frame):
        started = time.perf_counter()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.timeout, connect=2), transport=self.transport, trust_env=False
            ) as client:
                response = client.post(self.endpoint, json={"image": base64.b64encode(frame.jpeg).decode("ascii")})
                response.raise_for_status()
                return self.convert(response.json(), (time.perf_counter() - started) * 1000)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise InferenceError(f"Chipoint: {exc}") from exc
