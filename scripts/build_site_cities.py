"""把 Python 侧的城市坐标表编译成站点函数用的 TypeScript 模块。

为什么需要它：Open-Meteo 的免费地名接口只认拉丁/拼音，`name=泉州` 返回空。
Qoder Sites 的边缘函数里没有高德 Key（也不该把 Key 放进公开站点），
所以全国市/县坐标只能从项目自己的知识库导出一份，随函数一起打包。

用法（改过 knowledge/cities.py 或 divisions.json 之后重跑一次）：
    .venv\\Scripts\\python.exe scripts\\build_site_cities.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tripagent.knowledge.cities import all_cities  # noqa: E402

TARGET = ROOT / "sites" / "functions" / "cities.ts"


def main() -> int:
    rows = []
    for c in all_cities():
        if not c.name or c.lat is None or c.lng is None:
            continue
        rows.append({
            "n": c.name,
            "p": c.province or "",
            "a": c.adcode or "",
            "lat": round(c.lat, 4),
            "lng": round(c.lng, 4),
        })
    rows.sort(key=lambda r: r["n"])
    out = (
        "/** 全国地级市与县城坐标表（由 scripts/build_site_cities.py 从 knowledge/cities.py 生成，勿手改）。 */\n"
        "export type City = { n: string; p: string; a: string; lat: number; lng: number };\n"
        f"export const CITIES: City[] = {json.dumps(rows, ensure_ascii=False, separators=(',', ':'))};\n"
    )
    TARGET.write_text(out, encoding="utf-8")
    print(f"写入 {TARGET}：{len(rows)} 个城市，{len(out) / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
