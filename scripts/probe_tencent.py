"""核对腾讯 Web 服务的真实返回结构（Key 只从环境/文件读，绝不打印）。

用法（项目根目录，先把 TENCENT_MAP_KEY 写进 api_keys/tripagent.env）：
    .venv\\Scripts\\python.exe scripts\\probe_tencent.py 成都 大龙燚火锅
    .venv\\Scripts\\python.exe scripts\\probe_tencent.py 泉州 源记巷内牛肉馆

为什么需要它：腾讯 place search 的评分/营业时间/人均字段名会随 POI 类型与套餐变化，
我按多个可能字段名做了兼容解析，但"兼容"不等于"验证过"。
填好 Key 后跑一次这条命令，就能看到原始字段，确认解析有没有真的取到值。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tripagent.config import reload_settings          # noqa: E402
from tripagent.knowledge.cities import resolve_city   # noqa: E402
from tripagent.providers import Chain                 # noqa: E402
from tripagent.providers.tencent import _search_raw, pick_best  # noqa: E402


def main() -> int:
    city_name = sys.argv[1] if len(sys.argv) > 1 else "成都"
    keyword = sys.argv[2] if len(sys.argv) > 2 else "春熙路"
    s = reload_settings()
    if not s.tencent_map_key:
        print("没读到 TENCENT_MAP_KEY。请写进 api_keys/tripagent.env（不要粘贴到对话里）。")
        return 1
    city = resolve_city(city_name)
    if not city:
        print(f"内建城市表里没有「{city_name}」，换个城市名试试。")
        return 1

    print(f"城市={city.name}({city.adcode})  关键词={keyword}  Key=已读取(长度 {len(s.tencent_map_key)})")
    raw = _search_raw(s, city, keyword)
    if raw is None:
        return 1
    print(f"status={raw.get('status')} message={raw.get('message')} 命中={len(raw.get('data') or [])}")
    rows = raw.get("data") or []
    for r in rows[:3]:
        if not isinstance(r, dict):
            continue
        print(f"\n  ▸ {r.get('title')}")
        print(f"    全部字段: {sorted(r.keys())}")
        for k in ("rating", "comment_score", "star", "open_time", "opentime",
                  "average_price", "avg_price", "category", "address"):
            if k in r:
                print(f"    {k} = {r[k]}")
    best = pick_best(rows, keyword)
    print(f"\n名字匹配结果: {best.get('title') if best else '(没匹配上——说明关键词与店名差异太大)'}")

    ch = Chain(s)
    got = ch.run("poi_crosscheck", "check", city, [keyword])
    print(f"provider 解析结果: source={got.source} -> {got.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
