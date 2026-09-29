"""Сравнение сохранённых раундов с новым накоплением. Исходная БД открывается только для чтения."""

import argparse
import json
import sqlite3
from pathlib import Path

from streetjev.domain.evidence import Distribution, Frame, GeoEvidence
from streetjev.domain.round import GeoRound
from streetjev.infrastructure.catalog import load_geography


def replay(database: Path) -> list[dict]:
    catalog, _ = load_geography()
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    reports = []
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
                )
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
    args = parser.parse_args()
    if args.output.resolve() == args.database.resolve():
        parser.error("Выходной файл не может заменять исходную БД")
    result = replay(args.database)
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
