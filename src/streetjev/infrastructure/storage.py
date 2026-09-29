import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from streetjev.domain.evidence import Frame, GeoEvidence
from streetjev.domain.round import RoundSnapshot


def snapshot_json(snapshot: RoundSnapshot) -> str:
    data = asdict(snapshot)
    data["observed_regions"] = sorted(snapshot.observed_regions)
    data["last_observed_regions"] = sorted(snapshot.last_observed_regions)
    return json.dumps(data, ensure_ascii=False)


class SqliteRoundRepository:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS rounds (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, snapshot TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                frame_id TEXT PRIMARY KEY, round_id TEXT NOT NULL REFERENCES rounds(id),
                captured_at TEXT NOT NULL, source TEXT NOT NULL, expert TEXT NOT NULL,
                model_version TEXT NOT NULL, novelty REAL NOT NULL, elapsed_ms REAL NOT NULL,
                evidence TEXT NOT NULL, raw_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS observations_round ON observations(round_id);
        """)

    def start(self, snapshot: RoundSnapshot) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO rounds VALUES (?, ?, ?)",
                (snapshot.id, datetime.now(UTC).isoformat(), snapshot_json(snapshot)),
            )

    def append(self, snapshot: RoundSnapshot, frame: Frame, evidence: GeoEvidence, novelty: float) -> None:
        data = {
            "countries": dict(evidence.countries.probabilities),
            "unknown": evidence.countries.unknown,
            "regions": {
                k: {"probabilities": dict(v.probabilities), "unknown": v.unknown}
                for k, v in evidence.regions.items()
            },
        }
        with self.connection:
            self.connection.execute(
                "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    frame.id,
                    snapshot.id,
                    frame.captured_at,
                    frame.source,
                    evidence.source,
                    evidence.model_version,
                    novelty,
                    evidence.elapsed_ms,
                    json.dumps(data, ensure_ascii=False),
                    evidence.raw_json,
                ),
            )
            self.connection.execute(
                "UPDATE rounds SET snapshot = ? WHERE id = ?", (snapshot_json(snapshot), snapshot.id)
            )

    def close(self):
        self.connection.close()
