import json

import pytest

from streetjev.domain.evidence import Distribution, Frame, GeoEvidence
from streetjev.domain.geography import GeoCatalog, Place


@pytest.fixture
def catalog():
    return GeoCatalog(
        (Place("AT", "Austria", "Австрия"), Place("DE", "Germany", "Германия")),
        (
            Place("AT-7", "Tirol", "Тироль", "AT"),
            Place("AT-5", "Salzburg", "Зальцбург", "AT"),
            Place("DE-BY", "Bavaria", "Бавария", "DE"),
            Place("DE-HE", "Hessen", "Гессен", "DE"),
        ),
    )


def frame(value=0, identity="frame-1"):
    return Frame(identity, b"jpeg", bytes([value]) * 1024, "2026-09-30T00:00:00+00:00", "test")


def evidence(at=0.8, unknown=0, regions=None):
    return GeoEvidence(
        "test", "v1", Distribution({"AT": at, "DE": 1 - at}, unknown), regions or {}, json.dumps({"raw": True}), 10
    )
