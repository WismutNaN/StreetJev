"""Третий административный уровень. Поддержка только от координатных свидетельств."""

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class Subregion:
    code: str
    name: str
    country: str
    region: str | None


def subregion_support(points, assignments, places, countries, regions):
    masses = defaultdict(float)
    for point in points:
        code = assignments.get(point.code)
        place = places.get(code)
        if place and place.country == point.country:
            # Разные версии ADM1/ADM2 могут расходиться у границы. Не переносим
            # свидетельство из одного родительского региона в другой.
            if place.region and point.region and place.region != point.region:
                continue
            masses[code] += point.mass
    for parent, scores in (("region", regions), ("country", countries)):
        totals = defaultdict(float)
        for code, mass in masses.items():
            totals[getattr(places[code], parent)] += mass
        for code in masses:
            key = getattr(places[code], parent)
            if key is not None and totals[key] > 0:
                masses[code] *= min(1, scores.get(key, 0) / totals[key])
    # Отсутствующие/непокрытые точки не перераспределяются в загруженные районы.
    return dict(masses)
