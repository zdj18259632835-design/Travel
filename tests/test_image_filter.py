"""实拍图的内容把关：类型闸、同名程度、内容信号。

起因是用户的一句"就给我看几辆白车？"。定位下来不是算法看不懂图，
而是我们把"宝塔山景区地下停车场"的照片当成了"宝塔山景区"的照片——
老代码用"名字互相包含"匹配，而"宝塔山景区"确实包含在"宝塔山景区地下停车场"里。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tripagent.providers.images import (  # noqa: E402
    amap_photos,
    content_rank,
    match_rank,
    scene_signals,
    typecode_ok,
)


def _img(pixels) -> bytes:
    from PIL import Image

    im = Image.new("RGB", (600, 450))
    for y in range(450):
        for x in range(0, 600, 60):
            im.paste(pixels(x, y), (x, y, x + 60, y + 60))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def test_parking_lot_poi_is_not_a_photo_source():
    """停车场的图再清晰也不是景点图——类型这一刀必须在打分之前。"""
    scenic = {"name": "宝塔山景区", "typecode": "110210"}
    parking = {"name": "宝塔山景区地下停车场", "typecode": "150904"}
    guesthouse = {"name": "枣园宾馆", "typecode": "100102"}
    office = {"name": "宝塔山景区游客中心", "typecode": "070201"}
    assert typecode_ok(scenic)
    assert not typecode_ok(parking)
    assert not typecode_ok(guesthouse)
    assert not typecode_ok(office)
    # 多类型：只要有一个落在允许大类里就算通过
    assert typecode_ok({"typecode": "110210|110204"})
    # 缺字段不判死刑（老接口与小 POI 经常没有 typecode）
    assert typecode_ok({"name": "某地"})
    assert typecode_ok({"typecode": ""})


def test_exact_name_outranks_containing_name():
    """同名程度分三档：完全同名 > 以查询开头 > 互相包含。"""
    assert match_rank("宝塔山景区", "宝塔山景区") == 3
    assert match_rank("宝塔山景区", "宝塔山景区东门广场") == 2      # 景区的一部分，可以要
    assert match_rank("宝塔山景区", "某某宝塔山景区管理处") == 1     # 只是包含
    assert match_rank("西湖", "杭州西湖风景名胜区") > 0
    assert match_rank("宽窄巷子", "春熙路步行街") == 0
    assert match_rank("", "任何东西") == 0
    # 关键排序：完全同名必须赢过"停车场"那种包含式候选
    rows = ["宝塔山景区地下停车场", "宝塔山景区", "宝塔山景区游客中心"]
    best = max(rows, key=lambda r: match_rank("宝塔山景区", r))
    assert best == "宝塔山景区", best


def test_scene_signals_separate_parking_from_scenery():
    """同一张 600x450：水泥地+车身 vs 天空+草地，指标必须分得开。"""
    gray = scene_signals(_img(lambda x, y: (232, 232, 234) if y % 120 < 90 else (205, 205, 210)))
    sky = scene_signals(_img(lambda x, y: (110, 165, 230) if y < 225 else (62, 142, 74)))
    assert gray["gray"] > 0.5, gray
    assert gray["skyveg"] < 0.05, gray
    assert sky["skyveg"] > 0.4, sky
    assert content_rank(600, 450, 900.0, gray["gray"], gray["skyveg"], gray["color"]) < \
        content_rank(600, 450, 300.0, sky["gray"], sky["skyveg"], sky["color"]), \
        "停车场图（边缘锐利、方差更高）不该排赢风景图"


def test_amap_photos_upgrades_to_original_and_keeps_https():
    urls = amap_photos({"photos": [{"url": "http://store.is.autonavi.com/showpic/abc"}]}, limit=2)
    assert urls and urls[0].startswith("https://") and "operate=original" in urls[0]


class _FakeChain:
    """只喂分数，不联网：filter_poi_photos 的规则要能被确定性测试。"""

    def __init__(self, scores):
        self.scores = scores

    def run(self, capability, method, urls, **kw):
        from tripagent.providers.base import ChainResult

        return ChainResult(value=self.scores, source="fake")


class _P:
    def __init__(self, photos):
        self.photos = photos


def test_filter_drops_blurry_and_small_and_ranks_by_content():
    """删除只依据硬事实；内容信号只决定谁排第一。

    这条测试记录了一次改设计：原本想让灰白占比也参与删除，
    在 358 张真实图上扫描阈值后发现误杀 7/204 真景点图只换来多拦 6/106 无聊图，
    于是删除权收回到"分辨率 + 清晰度"，内容信号只做排序。
    """
    from tripagent.config import get_settings
    from tripagent.providers.images import filter_poi_photos

    s = get_settings()
    good = "https://x/good.jpg"        # 风景：灰白低、天空高
    lot = "https://x/lot.jpg"          # 停车场形状：灰白高、几乎无天空植被
    blur = "https://x/blur.jpg"        # 糊图：方差过低
    tiny = "https://x/tiny.jpg"        # 小图：短边不够
    unknown = "https://x/unknown.jpg"  # 没量到：必须保留
    scores = {
        good: [1600, 1200, 800.0, 0.06, 0.42, 0.30],
        lot: [1600, 1200, 1200.0, 0.62, 0.02, 0.05],
        blur: [1600, 1200, 2.0, 0.05, 0.40, 0.30],
        tiny: [300, 200, 900.0, 0.05, 0.40, 0.30],
    }
    p = _P([lot, blur, tiny, unknown, good])
    dropped, touched = filter_poi_photos(_FakeChain(scores), s, [p])
    assert p.photos[0] == good, p.photos                 # 风景排第一，停车场图被压到最后
    assert lot in p.photos, "灰白图只降权，不删除（阈值扫描的结论）"
    assert blur not in p.photos and tiny not in p.photos
    assert p.photos.index(unknown) > p.photos.index(good)  # 没量到的保留，但不靠前
    assert dropped == 2 and touched == 1


def test_night_scene_survives_and_stays_ahead_of_flat_gray():
    """夜景灰白占比也高，但色彩与天空还在——排序不能把它打到停车场图后面。"""
    from tripagent.config import get_settings
    from tripagent.providers.images import filter_poi_photos

    s = get_settings()
    night = "https://x/night.jpg"
    flat = "https://x/flat.jpg"
    scores = {
        night: [1600, 1200, 400.0, 0.50, 0.30, 0.12],
        flat: [1600, 1200, 400.0, 0.50, 0.01, 0.04],
    }
    p = _P([flat, night])
    filter_poi_photos(_FakeChain(scores), s, [p])
    assert p.photos == [night, flat], p.photos


def test_ui_does_not_label_the_photo_source_anymore():
    """用户明确要求：图片上不要标注"高德公开图"。"""
    root = Path(__file__).resolve().parents[1]
    text = (root / "ui_render.py").read_text(encoding="utf-8")
    assert "高德公开图" not in text


def test_band_note_is_a_content_line_not_a_caption():
    """用户第二轮点名的就是这一行：分组说明"光照正合适……"还是灰小字。

    第一轮我只改了卡片正文，漏了 day_view 里的分组说明——
    凡是用户要读的句子都不该用 st.caption（12px、灰色、语义是"次要信息"）。
    """
    root = Path(__file__).resolve().parents[1]
    src = (root / "ui_render.py").read_text(encoding="utf-8")
    assert "st.caption(BAND_NOTE" not in src
    assert 'content_line(BAND_NOTE[band], "band")' in src
    css = (root / "ui_theme.py").read_text(encoding="utf-8")
    assert ".bandnote" in css and "#12201A" in css
    # 即时问答的推荐理由与避坑同样升级，不允许退回 caption
    app = (root / "app.py").read_text(encoding="utf-8")
    assert 'content_line("推荐理由：' in app
    assert 'st.caption("推荐理由' not in app


def test_image_cap_selector_matches_real_streamlit_markup():
    """CSS 选择器不能凭想象：stImage / stDialog 这些 testid 必须在已装的 Streamlit 里存在。"""
    import streamlit
    from pathlib import Path as _P

    root = _P(streamlit.__file__).parent / "static"
    js = list(root.rglob("*.js"))
    assert js, "没找到 Streamlit 前端 bundle，这个测试就没意义"
    blob = "".join(f.read_text(encoding="utf-8", errors="ignore") for f in js)
    for tid in ("stImage", "stImageCaption", "stDialog"):
        assert tid in blob, f"Streamlit 1.66 前端里没有 {tid}，CSS 会打不中"


def test_image_window_is_capped_and_not_cropped():
    """统一画幅：高度上限按视口给，且必须是 contain 语义（不许 cover 裁切）。"""
    css = (Path(__file__).resolve().parents[1] / "ui_theme.py").read_text(encoding="utf-8")
    assert "max-height: 46vh" in css
    assert "width: auto !important" in css
    assert "object-fit: cover" not in css.split("[data-testid=\"stImage\"] img")[1][:400]
