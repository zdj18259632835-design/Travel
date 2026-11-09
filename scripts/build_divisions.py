"""抓取全国行政区划，生成本地名表：让「任意市/县/乡镇」都能被认出来。

为什么需要它：内建坐标表只有 70 多个热门旅游城市，用户输入「阿勒泰」「泰宁」
这类地名时，前端下拉框里根本没有这一项，还没走到地理编码就已经失败了。
高德 /v3/config/district 一次调用即可返回 34 省 / 416 市 / 3614 区县，
落成 JSON 后 resolve_city 离线就能命中，且乡镇级由 /v3/geocode/geo 兜底。

用法（项目根目录）：
    .venv\\Scripts\\python.exe scripts\\build_divisions.py          # 到区县级
    .venv\\Scripts\\python.exe scripts\\build_divisions.py --depth 4  # 再往下到乡镇（约 4 万条，慢）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from tripagent.config import get_settings  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "divisions.json"
URL = "https://restapi.amap.com/v3/config/district"


def fetch(depth: int) -> dict:
    key = get_settings().amap_key
    if not key:
        raise SystemExit("[X] 未配置 AMAP_KEY（api_keys/tripagent.env）")
    params = {"key": key, "keywords": "中国", "subdistrict": depth, "extensions": "base"}
    print(f"[i] 请求行政区划 subdistrict={depth} …")
    r = httpx.get(URL, params=params, timeout=120)
    data = r.json()
    if str(data.get("status")) != "1" or not data.get("districts"):
        raise SystemExit(f"[X] 接口失败：{data.get('info')}({data.get('infocode')})")
    print(f"[i] 响应 {len(r.content) / 1024:.0f} KB")
    return data["districts"][0]


_SUFFIX = re.compile(r"(?:特别行政区|自治区|自治州|自治县|地区|盟|市|县|区|旗)$")
_ETHNIC = re.compile(r"^(.{2,4}?)[\u4e00-\u9fa5]*(?:族自治州|族自治县|自治区)$")


def aliases_of(name: str) -> set[str]:
    """地名的可输入形式。

    不能用 rstrip("市区县")：那是按**字符集合**剥，"潮州市"会被剥成"潮"，
    潮州/广州/杭州/苏州 全部失效。必须按词剥，并且要处理民族自治地名的简称
    （用户会说「湘西」而不是「湘西土家族苗族自治州」）。
    """
    out = {name}
    stripped = _SUFFIX.sub("", name)
    if len(stripped) >= 2:
        out.add(stripped)
    m = _ETHNIC.match(name)
    if m:
        out.add(m.group(1))
    return {a for a in out if len(a) >= 2}


def flatten(node: dict, trail: list[str], bucket: dict) -> None:
    name = str(node.get("name") or "").strip()
    adcode = str(node.get("adcode") or "").strip()
    level = str(node.get("level") or "")
    center = str(node.get("center") or "")
    if name and adcode and "," in center:
        lng_s, lat_s = center.split(",")[:2]
        path = trail + [name]
        try:
            entry = {
                "key": f"a{adcode}",
                "name": name,
                "adcode": adcode,
                "lat": round(float(lat_s), 5),
                "lng": round(float(lng_s), 5),
                "level": level,
                "province": path[0] if path else "",
                "path": ">/".join(path[1:]),
            }
        except ValueError:
            entry = None
        if entry:
            # 同名不覆盖：先入库的（通常是更高级别/更热门）优先
            for alias in aliases_of(name):
                bucket.setdefault(alias, entry)
    for child in node.get("districts") or []:
        flatten(child, trail + [name], bucket)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=3, help="3=区县级，4=含乡镇（数据量大很多）")
    args = ap.parse_args()

    root = fetch(args.depth)
    bucket: dict[str, dict] = {}
    t0 = time.time()
    flatten(root, [], bucket)
    uniq: dict[str, dict] = {}
    for alias, entry in bucket.items():
        rec = uniq.setdefault(entry["adcode"], {**entry, "aliases": []})
        if alias != rec["name"] and alias not in rec["aliases"]:
            rec["aliases"].append(alias)
    rows = sorted(uniq.values(), key=lambda r: (r["level"], r["adcode"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    by_level: dict[str, int] = {}
    for r in rows:
        by_level[r["level"]] = by_level.get(r["level"], 0) + 1
    print(f"[OK] 写入 {OUT.name}：{len(rows)} 条（含别名），耗时 {time.time() - t0:.1f}s")
    print("     层级分布：", by_level)
    for probe in ("阿勒泰", "泰宁", "凤凰", "潮州", "凯里", "漠河"):
        hit = bucket.get(probe)
        print(f"     校验 {probe:<4} → {hit['name']}({hit['adcode']}, {hit['level']})" if hit else f"     校验 {probe:<4} → 未命中")
    print("\n下一步：重启 streamlit 即可在城市输入框里直接键入这些地名。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
