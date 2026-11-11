"""图片内容特征探针：用真实高德 POI 图找"能区分景区图与停车场图"的可用指标。

这一步是"先量再定阈值"。不做这一步，任何阈值都是审美猜测。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import io
import statistics

import httpx
import numpy as np
from PIL import Image

from tripagent.config import reload_settings
from tripagent.knowledge.cities import resolve_city
from tripagent.providers.base import Chain
from tripagent.providers.images import AMapImages

TARGETS = [
    ("延安", ["宝塔山", "延安革命纪念馆", "南泥湾", "枣园", "杨家岭革命旧址"]),
    ("成都", ["宽窄巷子景区", "成都武侯祠博物馆", "杜甫草堂", "春熙路"]),
    ("杭州", ["西湖", "灵隐寺", "西溪国家湿地公园"]),
    ("长沙", ["橘子洲", "岳麓山", "湖南省博物馆"]),
    ("北京", ["故宫", "八达岭长城", "南锣鼓巷"]),
]


def _box(m: "np.ndarray", r: int) -> "np.ndarray":
    """可分离滑动均值（替代 cv2.blur）：谱残差里那个邻域平均就是它。

    用前缀和算，形状必须逐段核对——上一版这里因为切片长度差 1 直接广播失败。
    """
    h, w = m.shape
    p = np.pad(m, ((r, r), (0, 0)), mode="edge")
    c = np.concatenate([np.zeros((1, w)), np.cumsum(p, axis=0)], axis=0)
    out = (c[2 * r + 1: 2 * r + 1 + h] - c[:h]) / (2 * r + 1)
    q = np.pad(out, ((0, 0), (r, r)), mode="edge")
    c2 = np.concatenate([np.zeros((h, 1)), np.cumsum(q, axis=1)], axis=1)
    return (c2[:, 2 * r + 1: 2 * r + 1 + w] - c2[:, :w]) / (2 * r + 1)


def feats(data: bytes) -> dict:
    im = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = im.size
    im.thumbnail((160, 160))
    a = np.asarray(im, dtype=np.float32) / 255.0
    hsv = np.asarray(im.convert("HSV"), dtype=np.float32) / 255.0
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]

    sky = ((hue > 0.52) & (hue < 0.75) & (sat < 0.65) & (val > 0.55)).mean()
    veg = ((hue > 0.18) & (hue < 0.46) & (sat > 0.22) & (val > 0.12)).mean()
    gray = ((sat < 0.10) & (val > 0.55)).mean()          # 车身/水泥/玻璃幕墙
    dark = (val < 0.10).mean()

    # 色彩熵：量化到 4bit/通道后算信息量
    q = (a * 15).astype(np.int64)
    idx = (q[..., 0] * 256 + q[..., 1] * 16 + q[..., 2]).ravel()
    counts = np.bincount(idx, minlength=4096).astype(np.float64)
    p = counts[counts > 0] / counts.sum()
    ent = float(-(p * np.log2(p)).sum())

    # 边缘方向：水平/垂直梯度强度比（Sobel 的简化版）
    g = a[..., 0]
    gx = np.abs(np.diff(g, axis=1))[1:, :]      # (H-1, W-1)
    gy = np.abs(np.diff(g, axis=0))[:, :-1]     # (H-1, W-1)
    ratio = float((gy.mean() + 1e-6) / (gx.mean() + 1e-6))   # >1 = 横向边多（地面/车顶）
    mag = np.hypot(gx, gy)
    edge = float((mag > 0.08).mean())

    # 谱残差显著性（ Hou 等人的 SR 公开做法，用 FFT + 可分离均值滤波实现）
    f = np.fft.fftshift(np.fft.fft2(g))
    log_amp = np.log(np.abs(f) + 1e-8)
    phase = np.angle(f)
    sal = np.abs(np.fft.ifft2(np.fft.ifftshift(np.exp(log_amp - _box(log_amp, 3) + 1j * phase)))) ** 2
    sal = sal / (sal.max() + 1e-8)
    sp = sal.ravel() / (sal.sum() + 1e-8)
    sal_ent = float(-(sp * np.log2(sp + 1e-10)).sum())

    # 顶部 1/3 的天空占比：真正的风景照上方常有天空
    top = hsv[: hsv.shape[0] // 3]
    top_sky = float(((top[..., 0] > 0.5) & (top[..., 2] > 0.5)).mean())

    return dict(w=w, h=h, sky=round(float(sky), 3), veg=round(float(veg), 3), gray=round(float(gray), 3),
                dark=round(float(dark), 3), ent=round(ent, 2), ratio=round(ratio, 2),
                edge=round(edge, 3), sal=round(sal_ent, 2), top_sky=round(top_sky, 3))


def main() -> int:
    s = reload_settings()
    chain = Chain(s)
    prov = AMapImages(s, chain.cache)
    rows = []
    for city_name, names in TARGETS:
        city = resolve_city(city_name)
        got = prov.fetch_batch(city, names, per=4)
        for name, urls in got.items():
            for u in urls:
                try:
                    r = httpx.get(u, timeout=8.0, follow_redirects=True,
                                  headers={"User-Agent": s.http_user_agent})
                    if r.status_code >= 400:
                        continue
                    f = feats(r.content)
                except Exception as exc:
                    print("跳过", name, type(exc).__name__, str(exc)[:60])
                    continue
                rows.append((city_name, name, u.split("/")[-1][:18], f))
    print(f"{'城市':<5}{'地点':<16}{'尺寸':<11}{'天空':<7}{'植被':<7}{'灰白':<7}{'熵':<7}{'横/纵':<7}{'边缘':<7}{'显著':<7}")
    for c, n, _u, f in rows:
        print(f"{c:<6}{n[:15]:<17}{str(f['w'])+'x'+str(f['h']):<12}"
              f"{f['sky']:<8}{f['veg']:<8}{f['gray']:<8}{f['ent']:<8}{f['ratio']:<8}{f['edge']:<8}{f['sal']:<8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
