"""Одна компактная иерархия стран, регионов и координатных совпадений."""

from dataclasses import dataclass

from streetjev.domain.localization import strongest_point


@dataclass(frozen=True)
class RankedPlace:
    kind: str
    code: str
    label: str
    country: str


def ranked_places(catalog, scores, subregions=None, support=None, assignments=None):
    subregions, support, assignments = subregions or {}, support or {}, assignments or {}
    if not scores.active:
        return []
    result = []
    for country in sorted(scores.countries, key=scores.countries.get, reverse=True)[:3]:
        if scores.countries[country] <= 0:
            continue
        result.append(RankedPlace("country", country, catalog.countries[country].label, country))
        point = strongest_point(scores.points, country)
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
                children = [s for s in subregions.values() if s.region == code and support.get(s.code, 0) > 0]
                if children:
                    child = max(children, key=lambda s: support[s.code])
                    result.append(RankedPlace("subregion", child.code, "      " + child.name, country))
                if point and point.region == code:
                    indent = "         " if children and assignments.get(point.code) == child.code else "      "
                    result.append(
                        RankedPlace(
                            "point", point.code, f"{indent}● {point.latitude:.3f}, {point.longitude:.3f}", country
                        )
                    )
        outside = [
            s
            for s in subregions.values()
            if s.country == country and s.region not in regions and support.get(s.code, 0) > 0
        ]
        if outside:
            child = max(outside, key=lambda s: support[s.code])
            parent = catalog.regions.get(child.region)
            label = f"{parent.label} / {child.name}" if parent else child.name
            result.append(RankedPlace("subregion", child.code, "   " + label, country))
        if point and point.region not in regions:
            result.append(
                RankedPlace("point", point.code, f"   ● {point.latitude:.3f}, {point.longitude:.3f}", country)
            )
    return result


def row_score(row, scores, subregions=None):
    if not scores.active:
        return None
    if row.kind == "country":
        return scores.countries.get(row.code, 0)
    if row.kind == "region":
        return scores.regions.get(row.code, 0) if row.country in scores.observed_regions else None
    if row.kind == "subregion":
        return (subregions or {}).get(row.code)
    return next((p.mass for p in scores.points if p.code == row.code), None)
