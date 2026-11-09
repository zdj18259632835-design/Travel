"""证据层：网友口碑、文旅宣传文案、避坑提示的统一载体。

行程里的每一条"为什么推荐"都应能回溯到 EvidenceRecord，
这是本项目对抗"AI 瞎编攻略"的核心设计（参考 TRLP 的 GraphRAG 溯源思路）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EvidenceKind = Literal["note", "review", "promo", "official", "model_summary", "fixture"]


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    city: str = ""
    poi_name: str = ""
    kind: EvidenceKind = "note"
    title: str = ""
    snippet: str = ""
    url: str = ""
    author_type: str = ""
    published_at: str = ""
    source: str = "fixture"
    confidence: float = 0.5

    # 提纯后的结构化结论（借鉴 TripStar 的"UGC → 结构化"步骤）
    extracted: dict[str, Any] = Field(default_factory=dict)
    # 约定键：stay_minutes / need_reservation / reservation_channel / pitfalls[] / photo_spots[] / best_time / verdict
    tags: list[str] = Field(default_factory=list)

    def brief(self, limit: int = 120) -> str:
        text = (self.snippet or self.title).replace("\n", " ").strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"
