"""API 探活脚本：改完 provider 后先跑它，再跑引擎。

用法（在项目根目录）：
    set PYTHONIOENCODING=utf-8 && .venv\\Scripts\\python.exe scripts\\probe_apis.py 成都
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tripagent.config import get_settings  # noqa: E402
from tripagent.knowledge.cities import resolve_city  # noqa: E402
from tripagent.providers import Chain  # noqa: E402


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "成都"
    city = resolve_city(name)
    if not city:
        print(f"[X] 认不出城市「{name}」，请加入 data/fixtures/cities_extra.json")
        return 1
    s = get_settings()
    print(f"[i] 城市={city.name} key={city.key} adcode={city.adcode} lat={city.lat} lng={city.lng} offline={s.offline}")
    chain = Chain(s)

    fr = chain.run("forecast", "fetch_forecast", city, 16)
    print(f"\n[forecast] source={fr.source} 天数={len(fr.value) if fr.value else 0}")
    for line in [str(a) for a in fr.attempts]:
        print("   ", line)
    for day in (fr.value or [])[:5]:
        print(f"    {day.date} {day.text:<6} {day.tmin_c}~{day.tmax_c}℃ 降水{day.precip_mm}mm 风{day.wind_max_kmh}km/h")

    ar = chain.run("alarm", "fetch_alarms", city, allow_empty=True)
    print(f"\n[alarm] source={ar.source} 条数={len(ar.value) if ar.value else 0}")
    for line in [str(a) for a in ar.attempts]:
        print("   ", line)
    for a in (ar.value or [])[:5]:
        print(f"    [{a.level}] {a.alarm_type} | {a.title[:40]} | {a.issuer[:16]} | {a.text[:60]}")

    gr = chain.run("guide", "fetch_guide", city)
    print(f"\n[guide] source={gr.source} keys={list(gr.value)[:6] if gr.value else '无'}")

    lr = Chain(s).providers_for("llm")
    for nm, inst in lr:
        ok, msg = inst.describe()
        print(f"[llm:{nm}] {'可用' if ok else '不可用'} —— {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
