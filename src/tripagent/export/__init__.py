"""导出层：同一份 TripPlan → Markdown / HTML / ICS。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..config import Settings, get_settings
from ..models.plan import TripPlan
from .html import build_context, to_html
from .ics import to_ics
from .markdown import to_markdown

_EXT = {"md": ".md", "html": ".html", "ics": ".ics"}


def export_plan(
    plan: TripPlan,
    stem: str = "",
    formats: tuple[str, ...] = ("md", "html", "ics"),
    settings: Settings | None = None,
) -> dict[str, Path]:
    settings = settings or get_settings()
    settings.ensure_dirs()
    stem = stem or f"{plan.request.destination}_{len(plan.days)}天_{datetime.now():%m%d_%H%M}"
    out: dict[str, Path] = {}
    for fmt in formats:
        if fmt not in _EXT:
            continue
        path = settings.output_dir / f"{stem}{_EXT[fmt]}"
        text = {"md": to_markdown, "html": to_html, "ics": to_ics}[fmt](plan)
        path.write_text(text, encoding="utf-8")
        out[fmt] = path
    return out


__all__ = ["export_plan", "to_html", "to_ics", "to_markdown", "build_context"]
