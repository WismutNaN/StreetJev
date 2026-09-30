"""Две мягкие проверки совместимости; повторные вопросы не считаются новыми кадрами."""

from dataclasses import dataclass, replace

from streetjev.domain.evidence import Distribution, GeoEvidence


@dataclass(frozen=True)
class PlaceClues:
    driving_side: str | None = None
    hemisphere: str | None = None


def compatibility(facts: PlaceClues, clues, driving_strength=0.45, hemisphere_strength=0.25) -> float:
    factor = 1.0
    for name, expected, strength in (
        ("driving_side", facts.driving_side, driving_strength),
        ("hemisphere", facts.hemisphere, hemisphere_strength),
    ):
        answer = clues.get(name)
        if expected is None or answer is None:
            continue
        opposite = {"left": "right", "right": "left", "north": "south", "south": "north"}[expected]
        contradiction = max(0.0, 2 * answer.probabilities.get(opposite, 0) - 1)
        factor *= 1 - strength * (1 - answer.unknown) * contradiction
    return factor


def apply_clues(
    evidence: GeoEvidence, clues, facts: dict[str, PlaceClues], driving_strength=0.45, hemisphere_strength=0.25
) -> GeoEvidence:
    def adjust(distribution):
        values = {
            code: score
            * compatibility(facts.get(code, PlaceClues()), clues, driving_strength, hemisphere_strength)
            for code, score in distribution.probabilities.items()
        }
        # Снятая поддержка становится неопределённостью, а не уверенностью в оставшихся странах.
        unknown = 1 - (1 - distribution.unknown) * sum(values.values())
        return Distribution(values, min(1.0, max(distribution.unknown, unknown)))

    return replace(
        evidence,
        countries=adjust(evidence.countries),
        regions={code: adjust(value) for code, value in evidence.regions.items()},
        clues=clues,
        cells=tuple(
            replace(
                cell,
                mass=cell.mass
                * compatibility(
                    facts.get(cell.country, PlaceClues()), clues, driving_strength, hemisphere_strength
                )
                * compatibility(
                    facts.get(cell.region, PlaceClues()), clues, driving_strength, hemisphere_strength
                ),
            )
            for cell in evidence.cells
        ),
        points=tuple(
            replace(
                point,
                mass=point.mass
                * compatibility(
                    facts.get(point.country, PlaceClues()), clues, driving_strength, hemisphere_strength
                )
                * compatibility(
                    facts.get(point.region, PlaceClues()), clues, driving_strength, hemisphere_strength
                ),
            )
            for point in evidence.points
        ),
    )
