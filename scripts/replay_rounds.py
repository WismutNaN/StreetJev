"""Сравнение сохранённых раундов с новым накоплением. Исходная БД открывается только для чтения."""

import argparse
import json
import sqlite3
from pathlib import Path

from streetjev.domain.evidence import Distribution, Frame, GeoEvidence
from streetjev.domain.fusion import fuse_evidence
from streetjev.domain.localization import LocalCell, LocalPoint
from streetjev.domain.round import GeoRound
from streetjev.infrastructure.catalog import load_geography


def replay(database: Path, imajev_only=False) -> list[dict]:
    catalog, _ = load_geography()
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    reports = []
    has_supplements = (
        connection.execute("SELECT 1 FROM sqlite_master WHERE name='supplementary_observations'").fetchone()
        is not None
    )
    try:
        for round_id, saved in connection.execute("SELECT id, snapshot FROM rounds ORDER BY rowid"):
            stored = json.loads(saved)
            if not stored["frames"]:
                continue
            state = GeoRound(catalog)
            for frame_id, captured_at, source, expert, model, novelty, elapsed_ms, data, raw in connection.execute(
                "SELECT frame_id, captured_at, source, expert, model_version, novelty, elapsed_ms, evidence, raw_json "
                "FROM observations WHERE round_id = ? ORDER BY rowid",
                (round_id,),
            ):
                data = json.loads(data)
                evidence = GeoEvidence(
                    expert,
                    model,
                    Distribution(data["countries"], data["unknown"]),
                    {
                        code: Distribution(region["probabilities"], region["unknown"])
                        for code, region in data["regions"].items()
                    },
                    raw,
                    elapsed_ms,
                    cells=tuple(LocalCell(**cell) for cell in data.get("cells", [])),
                    points=tuple(LocalPoint(**point) for point in data.get("points", [])),
                )
                if has_supplements and not imajev_only:
                    row = connection.execute(
                        "SELECT expert, model_version, evidence, raw_json, elapsed_ms, weight "
                        "FROM supplementary_observations WHERE frame_id = ? AND active = 1",
                        (frame_id,),
                    ).fetchone()
                    if row:
                        expert2, model2, encoded2, raw2, elapsed2, weight2 = row
                        data2 = json.loads(encoded2)
                        other = GeoEvidence(
                            expert2,
                            model2,
                            Distribution(data2["countries"], data2["unknown"]),
                            {
                                code: Distribution(value["probabilities"], value["unknown"])
                                for code, value in data2["regions"].items()
                            },
                            raw2,
                            elapsed2,
                            cells=tuple(LocalCell(**cell) for cell in data2.get("cells", [])),
                            points=tuple(LocalPoint(**point) for point in data2.get("points", [])),
                        )
                        evidence = fuse_evidence(evidence, other, weight2)
                # Изображения не сохранены: используем записанный вес, novelty повторно не вычисляем.
                frame = Frame(frame_id, b"", frame_id.encode(), captured_at, source)
                state.accept(state.id, frame, evidence, novelty)
            old_leader = max(stored["countries"], key=stored["countries"].get)
            new_leader = max(state.countries, key=state.countries.get)
            reports.append(
                {
                    "round": round_id,
                    "frames": state.frames,
                    "old_algorithm": stored.get("aggregation", "tempered_bayes_v1"),
                    "old_leader": old_leader,
                    "old_score": stored["countries"][old_leader],
                    "old_leader_new_score": state.countries[old_leader],
                    "new_leader": new_leader,
                    "new_score": state.countries[new_leader],
                }
            )
    finally:
        connection.close()
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/rounds.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/confidence-replay.json"))
    parser.add_argument("--imajev-only", action="store_true", help="сравнить с основным экспертом без Chipoint")
    args = parser.parse_args()
    if args.output.resolve() == args.database.resolve():
        parser.error("Выходной файл не может заменять исходную БД")
    result = replay(args.database, args.imajev_only)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in result:
        print(
            f"{item['round'][:8]} · {item['frames']} кадров · "
            f"{item['old_leader']} {item['old_score']:.2%} → {item['old_leader_new_score']:.2%}; "
            f"новый лидер {item['new_leader']} {item['new_score']:.2%}"
        )


if __name__ == "__main__":
    main()
