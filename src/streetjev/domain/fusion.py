"""Объединение экспертов одного кадра до временного накопления."""

import json
from dataclasses import replace

from streetjev.domain.evidence import Distribution, GeoEvidence
from streetjev.domain.localization import combine_cells


def fuse_evidence(primary: GeoEvidence, secondary: GeoEvidence, secondary_weight: float = 0.35) -> GeoEvidence:
    if not 0 < secondary_weight < 1:
        raise ValueError("Вес второго эксперта должен быть между 0 и 1")
    if set(primary.countries.probabilities) != set(secondary.countries.probabilities):
        raise ValueError("Эксперты используют разные каталоги стран")
    experts = ((primary, 1 - secondary_weight), (secondary, secondary_weight))
    weights = [(e, weight * (1 - e.countries.unknown)) for e, weight in experts]
    known = sum(weight for _, weight in weights)
    countries = Distribution(
        {
            code: sum(weight * e.countries.probabilities[code] for e, weight in weights)
            for code in primary.countries.probabilities
        },
        unknown=max(0, 1 - known),
    )
    regions = {}
    for country in set(primary.regions) | set(secondary.regions):
        # Оба ответа условны на эту страну. Повторное умножение на её исходный рейтинг
        # подавляло бы ветвь imajev, которую нашли благодаря Chipoint.
        contributors = [(e.regions[country], weight) for e, weight in weights if country in e.regions]
        keys = set(contributors[0][0].probabilities)
        if any(set(d.probabilities) != keys for d, _ in contributors):
            raise ValueError("Эксперты используют разные каталоги регионов")
        total = sum(weight for _, weight in contributors)
        usable = sum(weight * (1 - d.unknown) for d, weight in contributors)
        regions[country] = Distribution(
            {
                code: sum(weight * (1 - d.unknown) * d.probabilities[code] for d, weight in contributors)
                for code in keys
            },
            unknown=max(0, 1 - usable / total) if total else 1,
        )
    return GeoEvidence(
        "late_fusion",
        "linear_experts_v2",
        countries,
        regions,
        json.dumps(
            {
                "primary": primary.source,
                "secondary": secondary.source,
                "secondary_weight": secondary_weight,
                "calibrated": False,
            }
        ),
        max(primary.elapsed_ms, secondary.elapsed_ms),
        cells=combine_cells(
            replace(cell, mass=cell.mass * weight) for expert, weight in experts for cell in expert.cells
        ),
    )
