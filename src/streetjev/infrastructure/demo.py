import json
from datetime import UTC, datetime
from uuid import uuid4

from streetjev.domain.evidence import Distribution, Frame, GeoEvidence
from streetjev.domain.geography import GeoCatalog


class DemoExpert:
    """Явно синтетический пример Австрия / Германия / Швейцария, без анализа экрана."""

    def __init__(self, catalog: GeoCatalog):
        self.catalog = catalog
        self.index = 0

    def frame(self) -> Frame:
        return Frame(
            uuid4().hex,
            b"",
            bytes([(self.index * 79) % 256]) * 1024,
            datetime.now(UTC).isoformat(),
            "Демонстрационные данные",
        )

    def infer(self, frame: Frame) -> GeoEvidence:
        steps = [(0.40, 0.32, 0.12), (0.58, 0.19, 0.09), (0.73, 0.12, 0.04), (0.65, 0.18, 0.05)]
        at, de, ch = steps[self.index % len(steps)]
        self.index += 1
        rest = (1 - at - de - ch) / max(1, len(self.catalog.countries) - 3)
        probabilities = {k: rest for k in self.catalog.countries}
        probabilities.update({k: v for k, v in {"AT": at, "DE": de, "CH": ch}.items() if k in probabilities})
        regions = {}
        for country in ("AT", "DE", "CH"):
            if country not in self.catalog.countries:
                continue
            places = self.catalog.regions_of(country)
            if places:
                values = {
                    p.code: (15.0 if p.name in ("Tirol", "Tyrol", "Bayern", "Bavaria", "Graubünden") else 1.0)
                    for p in places
                }
                regions[country] = Distribution(values, 0.15)
        return GeoEvidence(
            "demo",
            "synthetic-v1",
            Distribution(probabilities, 0.08),
            regions,
            json.dumps({"demo": True, "step": self.index}),
            0.0,
        )
