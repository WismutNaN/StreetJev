from dataclasses import dataclass

from streetjev.domain.localization import LocalCell, LocalPoint
from streetjev.domain.round import RoundSnapshot


@dataclass(frozen=True)
class DisplayScores:
    countries: dict[str, float]
    regions: dict[str, float]
    observed_regions: frozenset[str]
    active: bool
    cells: tuple[LocalCell, ...] = ()
    points: tuple[LocalPoint, ...] = ()


def display_scores(snapshot: RoundSnapshot, source: str = "round") -> DisplayScores:
    if source == "last":
        return DisplayScores(
            snapshot.last_countries,
            snapshot.last_regions,
            snapshot.last_observed_regions,
            bool(snapshot.last_countries) and snapshot.last_unknown < 1,
            snapshot.last_cells,
            snapshot.last_points,
        )
    return DisplayScores(
        snapshot.countries,
        snapshot.regions,
        snapshot.observed_regions,
        snapshot.effective_frames > 0,
        snapshot.cells,
        snapshot.points,
    )
