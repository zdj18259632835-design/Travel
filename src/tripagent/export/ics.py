"""把行程导出成 .ics，一键导入手机日历——借鉴 awesome-llm-apps 的 ai_travel_agent，
成本极低但"获得感"很强：行程不再是看一遍就忘的网页。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..models.common import fmt_cny
from ..models.plan import TripPlan

_CRLF = "\r\n"


def _fold(line: str) -> str:
    """RFC5545 要求单行 ≤75 字节，中文按 UTF-8 计算，超长用空格续行。"""
    raw = line.encode("utf-8")
    if len(raw) <= 74:
        return line
    parts: list[str] = []
    buf = ""
    size = 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        if size + b > (74 if not parts else 73):
            parts.append(buf)
            buf, size = ch, b
        else:
            buf += ch
            size += b
    if buf:
        parts.append(buf)
    return parts[0] + _CRLF + _CRLF.join(" " + p for p in parts[1:])


def _esc(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
        .replace("，", ",")
    )


def _stamp(d: date, minute: int) -> str:
    dt = datetime(d.year, d.month, d.day) + timedelta(minutes=minute)
    return dt.strftime("%Y%m%dT%H%M%S")


def to_ics(plan: TripPlan) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TripAgent//LifeHack Travel Planner 0.1//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold(f"X-WR-CALNAME:{_esc(plan.request.destination)}{len(plan.days)}天行程"),
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for day in plan.days:
        if day.date is None:
            continue
        for slot in day.slots:
            if slot.end_minute <= slot.start_minute:
                continue
            poi = slot.poi
            desc_parts = [f"{slot.type.label}"]
            if poi:
                desc_parts.append(f"类目：{poi.category.label}")
                if poi.rating:
                    desc_parts.append(f"评分：{poi.rating}")
                if slot.cost_cents:
                    desc_parts.append(f"花费：{fmt_cny(slot.cost_cents)}（含同行人数）")
                if poi.open_hours_raw:
                    desc_parts.append(f"营业：{poi.open_hours_raw}")
                if poi.why:
                    desc_parts.append(f"理由：{poi.why}")
                if poi.pitfalls:
                    desc_parts.append("避坑：" + "；".join(poi.pitfalls[:3]))
            if slot.reservation_hint:
                desc_parts.append(f"【{slot.reservation_hint}】")
            if slot.transit_before:
                tb = slot.transit_before
                desc_parts.append(f"交通：约 {tb.minutes} 分钟（{tb.summary or tb.mode}）")
            if day.weather:
                desc_parts.append(f"天气：{day.weather_brief}")
            uid = f"{plan.trip_id}-d{day.index}s{slot.seq}@tripagent"
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{_stamp(day.date, 0)}",
                f"DTSTART;TZID=Asia/Shanghai:{_stamp(day.date, slot.start_minute)}",
                f"DTEND;TZID=Asia/Shanghai:{_stamp(day.date, slot.end_minute)}",
                _fold(f"SUMMARY:Day{day.index} {_esc(slot.title)}"),
                _fold("LOCATION:" + _esc((poi.address or poi.district or poi.city) if poi else plan.request.destination)),
                _fold("DESCRIPTION:" + _esc("\n".join(desc_parts))),
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return _CRLF.join(lines) + _CRLF
