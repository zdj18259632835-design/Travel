"""SQLite 历史存储：行程可回溯、可打补丁，不必每次全量重算。

单文件库（data/tripagent.db），零部署成本；plan_version 不可变追加，
二次开发要做"改一天不动其余"的增量编辑时，直接基于旧版本 diff。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from ..config import Settings, get_settings
from ..models.plan import TripPlan

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    trip_id     TEXT PRIMARY KEY,
    city        TEXT NOT NULL,
    days        INTEGER NOT NULL,
    budget_cents INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    latest_version INTEGER NOT NULL DEFAULT 1,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS plan_versions (
    trip_id     TEXT NOT NULL,
    version     INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    plan_json   TEXT NOT NULL,
    PRIMARY KEY (trip_id, version)
);
CREATE INDEX IF NOT EXISTS idx_trips_created ON trips(created_at DESC);
"""


class TripStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.path: Path = self.settings.data_dir / "tripagent.db"
        self._conn = sqlite3.connect(str(self.path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------- 写 ----------
    def save(self, plan: TripPlan, reason: str = "初始生成") -> tuple[str, int]:
        plan.plan_version = self._next_version(plan.trip_id)
        now = datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            """INSERT INTO trips(trip_id, city, days, budget_cents, created_at, latest_version, note)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(trip_id) DO UPDATE SET latest_version=excluded.latest_version""",
            (
                plan.trip_id,
                plan.request.destination,
                len(plan.days),
                plan.request.budget_cents,
                plan.created_at.isoformat(timespec="seconds"),
                plan.plan_version,
                plan.request.raw_text[:200],
            ),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO plan_versions(trip_id, version, created_at, reason, plan_json) VALUES(?,?,?,?,?)",
            (plan.trip_id, plan.plan_version, now, reason, plan.model_dump_json()),
        )
        self._conn.commit()
        return plan.trip_id, plan.plan_version

    # ---------- 读 ----------
    def list(self, limit: int = 20) -> list[dict]:
        cur = self._conn.execute(
            "SELECT trip_id, city, days, budget_cents, created_at, latest_version, note FROM trips ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        keys = ("trip_id", "city", "days", "budget_cents", "created_at", "latest_version", "note")
        return [dict(zip(keys, row)) for row in cur.fetchall()]

    def load(self, trip_id: str, version: int | None = None) -> TripPlan | None:
        if version is None:
            cur = self._conn.execute(
                "SELECT plan_json FROM plan_versions WHERE trip_id=? ORDER BY version DESC LIMIT 1", (trip_id,)
            )
        else:
            cur = self._conn.execute(
                "SELECT plan_json FROM plan_versions WHERE trip_id=? AND version=?", (trip_id, version)
            )
        row = cur.fetchone()
        if not row:
            return None
        try:
            return TripPlan.model_validate(json.loads(row[0]))
        except Exception:
            return None

    def versions(self, trip_id: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT version, created_at, reason FROM plan_versions WHERE trip_id=? ORDER BY version", (trip_id,)
        )
        return [{"version": v, "created_at": c, "reason": r} for v, c, r in cur.fetchall()]

    def delete(self, trip_id: str) -> None:
        self._conn.execute("DELETE FROM plan_versions WHERE trip_id=?", (trip_id,))
        self._conn.execute("DELETE FROM trips WHERE trip_id=?", (trip_id,))
        self._conn.commit()

    def _next_version(self, trip_id: str) -> int:
        cur = self._conn.execute("SELECT MAX(version) FROM plan_versions WHERE trip_id=?", (trip_id,))
        row = cur.fetchone()[0]
        return int(row or 0) + 1
