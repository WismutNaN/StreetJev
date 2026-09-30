"""Одна компактная иерархия стран, регионов и координатных совпадений."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RankedPlace:
    kind: str
    code: str
    label: str
    country: str


def ranked_places(catalog, scores):
    if not scores.active:
        return []
    result = []
    for country in sorted(scores.countries, key=scores.countries.get, reverse=True)[:3]:
        if scores.countries[country] <= 0:
            continue
        result.append(RankedPlace("country", country, catalog.countries[country].label, country))
        points = [p for p in scores.points if p.country == country]
        point = max(points, key=lambda p: p.mass) if points else None
        regions = (
            sorted(
                (r.code for r in catalog.regions_of(country)),
                key=lambda code: scores.regions.get(code, 0),
                reverse=True,
            )[:2]
            if country in scores.observed_regions
            else []
        )
        for code in regions:
            if scores.regions.get(code, 0) > 0:
                result.append(RankedPlace("region", code, "   " + catalog.regions[code].label, country))
                if point and point.region == code:
                    result.append(
                        RankedPlace(
                            "point", point.code, f"      ● {point.latitude:.3f}, {point.longitude:.3f}", country
                        )
                    )
        if point and point.region not in regions:
            result.append(
                RankedPlace("point", point.code, f"   ● {point.latitude:.3f}, {point.longitude:.3f}", country)
            )
    return result


def row_score(row, scores):
    if not scores.active:
        return None
    if row.kind == "country":
        return scores.countries.get(row.code, 0)
    if row.kind == "region":
        return scores.regions.get(row.code, 0) if row.country in scores.observed_regions else None
    return next((p.mass for p in scores.points if p.code == row.code), None)
