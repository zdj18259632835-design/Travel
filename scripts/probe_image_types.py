"""用生产打分函数（score_url）按高德 typecode 分组量真实图，定淘汰阈值。

之前那版探针用的是探针自己的特征函数，和生产不一致；
阈值如果按探针定，上线就等于没验证过。这一版直接调 score_url。
"""

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from tripagent.config import reload_settings
from tripagent.providers.images import score_url

QUERIES = [
    ("延安", ["宝塔山景区", "枣园革命旧址", "杨家岭革命旧址", "延安革命纪念馆", "南泥湾"]),
    ("成都", ["宽窄巷子景区", "杜甫草堂", "武侯祠", "春熙路", "建设巷"]),
    ("杭州", ["西湖", "灵隐寺", "河坊街", "西溪国家湿地公园"]),
    ("长沙", ["橘子洲", "岳麓山", "太平街", "湖南省博物馆"]),
    ("北京", ["故宫", "八达岭长城", "南锣鼓巷", "三里屯"]),
    ("西安", ["大雁塔", "回民街", "城墙", "永兴坊"]),
]
GROUP = {"11": "风景名胜", "05": "餐饮", "06": "购物", "08": "体育休闲", "14": "科教文化",
         "15": "交通设施", "07": "生活服务", "10": "住宿", "12": "商务住宅", "13": "政府",
         "19": "地名地址", "01": "汽车服务"}


def main() -> int:
    s = reload_settings()
    rows: dict[str, list] = {}
    for city, kws in QUERIES:
        for kw in kws:
            r = httpx.get("https://restapi.amap.com/v5/place/text",
                          params={"key": s.amap_key, "keywords": kw, "region": city,
                                  "citylimit": "true", "page_size": 8, "show_fields": "photos"},
                          timeout=12, headers={"User-Agent": s.http_user_agent})
            for p in (r.json().get("pois") or []):
                tc = str(p.get("typecode") or "").split("|")[0][:2]
                for x in (p.get("photos") or [])[:2]:
                    u = str(x.get("url") or "").replace("http://", "https://", 1)
                    sc = score_url(u) if u.startswith("https") else None
                    if not sc:
                        continue
                    rows.setdefault(GROUP.get(tc, tc), []).append(
                        (float(sc[3]), float(sc[4]), float(sc[5]), float(sc[2]), str(p.get("name"))[:18]))
    print(f"{'类型':<10}{'n':<5}{'灰白中位':<10}{'灰白P90':<10}{'天空植被中位':<14}{'色彩中位':<10}{'会误杀几张':<12}")
    for g, v in sorted(rows.items(), key=lambda x: -len(x[1])):
        grays = sorted(x[0] for x in v)
        sv = sorted(x[1] for x in v)
        col = sorted(x[2] for x in v)
        # 候选淘汰线：灰白 ≥0.35 且 天空植被 ≤0.08 且 色彩 ≤0.10
        killed = [x for x in v if x[0] >= 0.35 and x[1] <= 0.08 and x[2] <= 0.10]
        print(f"{g:<11}{len(v):<5}{statistics.median(grays):<10.3f}{grays[int(len(grays)*0.9)]:<10.3f}"
              f"{statistics.median(sv):<14.3f}{statistics.median(col):<10.3f}{len(killed):<6}"
              f"{' '.join(x[4] for x in killed[:2])}")

    # 阈值扫描：删掉"没内容的灰白图"和"误杀真景点图"是同一个旋钮的两端，
    # 只看一个数决定不了，必须把两端一起摆出来。
    scenic = rows.get("风景名胜", []) + rows.get("地名地址", []) + rows.get("科教文化", [])
    boring = rows.get("生活服务", []) + rows.get("交通设施", []) + rows.get("住宿", [])
    print(f"\n{'灰白线':<9}{'天空植被线':<13}{'色彩线':<9}{'误杀真景点':<12}{'命中无聊图':<12}")
    for gline in (0.35, 0.40, 0.45, 0.50):
        for sline in (0.05, 0.08):
            for cline in (0.08, 0.10):
                fk = sum(1 for x in scenic if x[0] >= gline and x[1] <= sline and x[2] <= cline)
                tk = sum(1 for x in boring if x[0] >= gline and x[1] <= sline and x[2] <= cline)
                print(f"{gline:<9.2f}{sline:<13.2f}{cline:<9.2f}"
                      f"{fk}/{len(scenic):<10}{tk}/{len(boring)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
