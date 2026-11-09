"""天气能力 provider：数值预报 + 官方预警 + 离线兜底。

选型理由（2026-11 实测）：
- 中国天气网官方 data/sk、data/cityinfo 已停更，开放平台需付费 → 不用；
- Open-Meteo 免 key、可取 16 天，正好覆盖「最近半个月」诉求 → 主数值源；
- CMA / NMC 的预警接口免 key（需带 User-Agent），补「极端天气提示」→ 预警源；
- 和风天气有独立 API Host，作为「用户填了 key 就自动升级」的可选源；
- fixture 用气候学近似生成确定性数据 → 现场断网也能完整演示。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from ..config import get_settings
from ..knowledge.cities import CityInfo
from ..knowledge.wmo import describe
from ..models.enums import Severity
from ..models.weather import AlarmRecord, DayForecast, LEVEL_TO_SEVERITY
from .base import Provider, ProviderError, provider


def _parse_date(text: str) -> date | None:
    try:
        return datetime.strptime(str(text)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


@provider("forecast", "open_meteo")
class OpenMeteo(Provider):
    """免密钥 16 天预报。坑：只给 WMO 数字码，中文文案需自映射（见 knowledge/wmo.py）。"""

    BASE = "https://api.open-meteo.com/v1/forecast"

    def fetch_forecast(self, city: CityInfo, days: int = 16) -> list[DayForecast]:
        days = max(1, min(int(days), 16))
        params = {
            "latitude": round(city.lat, 4),
            "longitude": round(city.lng, 4),
            "daily": ",".join(
                [
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_sum",
                    "precipitation_probability_max",
                    "wind_speed_10m_max",
                    "relative_humidity_2m_max",
                    "uv_index_max",
                    "sunrise",
                    "sunset",
                ]
            ),
            "timezone": "Asia/Shanghai",
            "forecast_days": days,
        }

        def fetch() -> list[DayForecast]:
            payload = self.get_json(self.BASE, params)
            daily = (payload or {}).get("daily") or {}
            times = daily.get("time") or []
            out: list[DayForecast] = []
            for i, t in enumerate(times):
                d = _parse_date(t)
                if not d:
                    continue
                code = _num(daily.get("weather_code"), i)
                info = describe(int(code) if code is not None else None)
                out.append(
                    DayForecast(
                        date=d,
                        wmo_code=int(code) if code is not None else None,
                        text=info.zh,
                        text_en=info.en,
                        tmax_c=_num(daily.get("temperature_2m_max"), i),
                        tmin_c=_num(daily.get("temperature_2m_min"), i),
                        precip_mm=_num(daily.get("precipitation_sum"), i) or 0.0,
                        precip_prob=_num(daily.get("precipitation_probability_max"), i),
                        wind_max_kmh=_num(daily.get("wind_speed_10m_max"), i),
                        humidity_max=_num(daily.get("relative_humidity_2m_max"), i),
                        uv_index=_num(daily.get("uv_index_max"), i),
                        sunrise=str((daily.get("sunrise") or [""] * (i + 1))[i] or "")[11:16] or None,
                        sunset=str((daily.get("sunset") or [""] * (i + 1))[i] or "")[11:16] or None,
                        source="open_meteo",
                    )
                )
            if not out:
                raise ProviderError("open-meteo 返回空 daily 序列")
            return out

        # 字段集变了就换 key 版本：否则旧缓存里没有紫外线，新建议会莫名缺一条，
        # 排查起来还像是规则引擎的 bug。
        return self.cached_call(("forecast", "open_meteo", "v2", city.key, days), fetch, DayForecast)


def _num(seq: Any, i: int) -> float | None:
    if not isinstance(seq, list) or i >= len(seq):
        return None
    v = seq[i]
    return float(v) if isinstance(v, (int, float)) else None


@provider("forecast", "qweather")
@provider("alarm", "qweather")
class QWeather(Provider):
    """和风天气：需 key + 账号专属 Host（旧 devapi/api 域名现已 403 Invalid Host）。"""

    def available(self) -> bool:
        s = self.settings
        return bool(s.qweather_key and s.qweather_host)

    def describe(self) -> tuple[bool, str]:
        s = self.settings
        if not s.qweather_key:
            return False, "未配置 QWEATHER_KEY"
        if not s.qweather_host:
            return False, "已填 key，但缺 QWEATHER_HOST（控制台里那个专属 API Host）"
        return True, f"{s.qweather_host}（15 天预报 + 官方预警）"

    def _location_id(self, city: CityInfo) -> str:
        def fetch() -> Any:
            data = self.get_json(
                f"{self._host()}/geo/v2/city/lookup",
                {"location": city.name, "key": self.settings.qweather_key},
            )
            rows = (data or {}).get("location") or []
            if not rows:
                raise ProviderError("和风城市检索为空")
            return {"id": rows[0].get("id", "")}

        cached = self.cached_call(("qweather_geo", city.key), fetch)
        return (cached or {}).get("id", "") if isinstance(cached, dict) else str(cached)

    def _host(self) -> str:
        """控制台给的是裸域名（如 m8487vkq9b.re.qweatherapi.com），必须补协议头，
        否则 httpx 会报 "Request URL is missing an 'http://' or 'https://' protocol"。"""
        raw = (self.settings.qweather_host or "").strip().rstrip("/")
        if not raw:
            return raw
        if raw.startswith(("http://", "https://")):
            return raw
        return f"https://{raw}"

    def fetch_forecast(self, city: CityInfo, days: int = 16) -> list[DayForecast]:
        loc = self._location_id(city)
        if not loc:
            raise ProviderError("拿不到和风 LocationID")
        for path, n in (("/v7/weather/15d", 15), ("/v7/weather/7d", 7)):
            if n > days:
                continue
            data = self.get_json(
                f"{self._host()}{path}", {"location": loc, "key": self.settings.qweather_key}
            )
            rows = (data or {}).get("daily") or []
            out: list[DayForecast] = []
            for r in rows:
                d = _parse_date(r.get("fxDate", ""))
                if not d:
                    continue
                out.append(
                    DayForecast(
                        date=d,
                        text=r.get("textDay", "") or r.get("textNight", "") or "未知",
                        text_en=r.get("iconDay", ""),
                        tmax_c=_safe_float(r.get("tempMax")),
                        tmin_c=_safe_float(r.get("tempMin")),
                        precip_mm=_safe_float(r.get("precip")) or 0.0,
                        # 和风 15d/7d 没有"降水概率"，只有相对湿度。
                        # 以前把 humidity 塞进 precip_prob，结果"晴、湿度 90"被说成"可能下雨"，
                        # 用户一眼就觉得结论不可信 —— 拿不到的字段必须留 None。
                        precip_prob=None,
                        wind_max_kmh=_safe_float(r.get("windSpeedDay")),
                        humidity_max=_safe_float(r.get("humidity")),
                        uv_index=_safe_float(r.get("uvIndex")),
                        source="qweather",
                    )
                )
            if out:
                return out
        raise ProviderError("和风接口未返回 daily")

    def fetch_alarms(self, city: CityInfo) -> list[AlarmRecord]:
        loc = self._location_id(city)
        data = self.get_json(
            f"{self._host()}/v7/warning/now", {"location": loc, "key": self.settings.qweather_key},
            timeout=self.settings.alarm_timeout,
        )
        out = []
        for w in (data or {}).get("warning") or []:
            text = w.get("text", "")
            title = w.get("title", "")
            alarm_type, level = parse_alarm(f"{title} {text}")
            level = level or str(w.get("level", ""))
            out.append(
                AlarmRecord(
                    title=title,
                    alarm_type=alarm_type,
                    level=level,
                    severity=LEVEL_TO_SEVERITY.get(level, Severity.WATCH),
                    scope="city",
                    issuer=w.get("publisher", "") or "和风天气",
                    issued_at=w.get("startTime", "") or w.get("pubTime", ""),
                    area=city.name,
                    text=text,
                    source="qweather",
                )
            )
        return out


@provider("alarm", "cma_alarm")
class CMAAlarm(Provider):
    """中国气象局公开预警接口。必须带 User-Agent，否则 403。

    真实结构：{"msg":"success","code":0,"data":[{"id":"230229…","headline":…,"title":…,
    "description":…,"effective":"2026/11/05 20:35","type":"p0000003"}]}
    id 前 6 位是县级 adcode，用它做地域过滤比按地名文本匹配可靠得多。
    """

    URL = "https://weather.cma.cn/api/alarm"

    def describe(self) -> tuple[bool, str]:
        return True, "weather.cma.cn/api/alarm（全国实时预警，免 key）"

    def fetch_alarms(self, city: CityInfo) -> list[AlarmRecord]:
        def fetch() -> list[AlarmRecord]:
            data = self.get_json(self.URL, headers={"Accept": "application/json"},
                                 timeout=self.settings.alarm_timeout)
            rows = (data or {}).get("data") if isinstance(data, dict) else None
            return select_alarms(rows or [], city, "cma_alarm")

        return self.cached_call(("alarm", "cma_alarm", city.key), fetch, AlarmRecord)


@provider("alarm", "nmc_alarm")
class NMCAlarm(Provider):
    """中央气象台 findAlarm：{"data":{"page":{"count":454,"list":[{alertid,title,issuetime,url,pic}]}}}"""

    URL = "https://www.nmc.cn/rest/findAlarm"

    def describe(self) -> tuple[bool, str]:
        return True, "nmc.cn/rest/findAlarm（免 key，标题自带类型与级别）"

    def fetch_alarms(self, city: CityInfo) -> list[AlarmRecord]:
        params = {
            "pageNo": 1,
            "pageSize": 200,
            "signaltype": "",
            "signallevel": "",
            "province": "",
            "city": "",
            "county": "",
        }

        def fetch() -> list[AlarmRecord]:
            data = self.get_json(self.URL, params, timeout=self.settings.alarm_timeout)
            page = ((data or {}).get("data") or {}).get("page") or {}
            rows = [r for r in (page.get("list") or []) if isinstance(r, dict)]
            for r in rows:
                if str(r.get("url", "")).startswith("/"):
                    r["url"] = "https://www.nmc.cn" + r["url"]
            return select_alarms(rows, city, "nmc_alarm")

        return self.cached_call(("alarm", "nmc_alarm", city.key), fetch, AlarmRecord)


@provider("forecast", "fixture")
@provider("alarm", "fixture")
class FixtureWeather(Provider):
    """离线兜底：按纬度 + 月份做气候学近似，同城市同日期结果稳定可复现。"""

    def fetch_forecast(self, city: CityInfo, days: int = 16) -> list[DayForecast]:
        today = date.today()
        out: list[DayForecast] = []
        for i in range(days):
            d = today + timedelta(days=i)
            rng = _stable_rng(f"{city.key}-{d.isoformat()}")
            base = _climate_mean(city.lat, d.month)
            amp = 4.5 + 6.0 * abs(city.lat) / 60
            tmax = round(base + amp * 0.6 + rng() * 3.0, 1)
            tmin = round(base - amp * 0.6 - rng() * 2.0, 1)
            wet_lik = _wet_probability(city.lat, d.month)
            roll = rng()
            if roll < wet_lik * 0.45:
                code, precip = 63, round(6 + rng() * 14, 1)
            elif roll < wet_lik:
                code, precip = 61, round(1 + rng() * 8, 1)
            elif roll < wet_lik + 0.12:
                code, precip = 3, 0.0
            else:
                code, precip = (0 if rng() > 0.4 else 2), 0.0
            if d.month in (7, 8, 9) and roll > 0.93:
                code = 95  # 夏季午后雷暴
            info = describe(code)
            out.append(
                DayForecast(
                    date=d,
                    wmo_code=code,
                    text=info.zh,
                    text_en=info.en,
                    tmax_c=tmax,
                    tmin_c=tmin,
                    precip_mm=precip,
                    precip_prob=int(min(95, max(5, roll * 100))),
                    wind_max_kmh=round(8 + rng() * 26, 1),
                    humidity_max=int(50 + rng() * 45),
                    uv_index=_uv_estimate(code, city.lat, d.month),
                    source="fixture",
                )
            )
        return out

    def fetch_alarms(self, city: CityInfo) -> list[AlarmRecord]:
        today = date.today()
        rng = _stable_rng(f"alarm-{city.key}-{today.isoformat()}")
        out: list[AlarmRecord] = []
        pool = _alarm_pool_by_month(today.month, city.lat)
        for name, level, text, sev in pool:
            if rng() < 0.12:  # 兜底模式只在少数情形造一条演练用预警，避免天天"台风"
                out.append(
                    AlarmRecord(
                        title=f"{city.name}市气象{level}预警：{name}",
                        alarm_type=name,
                        level=level,
                        issuer=f"{city.name}市气象台（离线演示数据）",
                        issued_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                        area=city.name,
                        text=text,
                        source="fixture",
                    )
                )
                if sev.rank >= Severity.WARN.rank:
                    break
        return out


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _uv_estimate(code: int, lat: float, month: int) -> float:
    """离线兜底的紫外线近似：晴天正午按纬度与季节估，云雨按比例打折。

    不是实测值，所以只在 fixture 源里出现；真实源（open_meteo / 和风）给的是接口原值。
    """
    clear = {0: 1.0, 1: 0.85, 2: 0.6, 3: 0.45}.get(code, 0.25)
    seasonal = {12: 0.45, 1: 0.5, 2: 0.6, 3: 0.75, 4: 0.9, 5: 1.0,
                6: 1.05, 7: 1.1, 8: 1.05, 9: 0.9, 10: 0.7, 11: 0.55}[month]
    base = 9.0 - 0.055 * max(0.0, abs(lat) - 20.0)
    return round(max(0.5, min(12.0, base * seasonal * clear)), 1)


def _stable_rng(seed: str):
    """确定性伪随机：不用 random 全局态，保证同一城市同一天每次生成一致。"""
    import hashlib

    state = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)

    def nxt() -> float:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % (2**64)
        return ((state >> 11) & 0x1FFFFF) / 0x200000

    def rng() -> float:
        return nxt()

    return rng


def _climate_mean(lat: float, month: int) -> float:
    """粗颗粒气候均值：以纬度决定年均温，以月份决定季节偏移。"""
    annual = 27.0 - 0.42 * abs(lat) + 4.0
    seasonal = {
        1: -10.5, 2: -8.0, 3: -2.5, 4: 3.5, 5: 8.5, 6: 12.0,
        7: 13.5, 8: 12.5, 9: 8.0, 10: 1.5, 11: -5.5, 12: -9.5,
    }[month]
    if abs(lat) < 25:  # 热带季节振幅小
        seasonal *= 0.35
    return annual + seasonal


def _wet_probability(lat: float, month: int) -> float:
    if month in (6, 7, 8, 9):
        return 0.55 if lat < 30 else 0.45
    if month in (12, 1, 2):
        return 0.22
    return 0.34


def _alarm_pool_by_month(month: int, lat: float) -> list[tuple[str, str, str, Severity]]:
    """按月构造演练预警池（等级/文案参考公开预警规范）。"""
    summer = [
        ("台风", "橙", "未来 24 小时受台风外围环流影响，沿海阵风 10 级以上，建议取消滨海与户外行程。", Severity.DANGER),
        ("暴雨", "橙", "12 小时降雨量将达 70 毫米以上，低洼路段可能积水，建议转为室内游览。", Severity.DANGER),
        ("高温", "橙", "日最高气温将达 38℃ 以上，11:00-16:00 避免长时间户外活动。", Severity.WARN),
        ("强对流", "黄", "可能出现短时强降水、雷暴大风，户外景点存在临时闭园风险。", Severity.WARN),
    ]
    winter = [
        ("寒潮", "橙", "48 小时最低气温将下降 10℃ 以上，需备足防寒衣物，山区道路可能结冰。", Severity.WARN),
        ("道路结冰", "橙", "路面温度低于 0℃ 且有降水，自驾与山区行程建议改期。", Severity.DANGER),
        ("大风", "黄", "阵风 8 级以上，索道、游船、玻璃栈道等项目可能停运。", Severity.WARN),
        ("暴雪", "橙", "12 小时降雪量将达 6 毫米以上，景区可能临时闭园。", Severity.DANGER),
    ]
    spring = [
        ("大风沙尘", "黄", "阵风 7 级以上并伴有扬沙，建议佩戴口罩、减少长时间户外暴露。", Severity.WATCH),
        ("倒春寒", "蓝", "短期降温明显，早晚温差大，注意添衣。", Severity.WATCH),
    ]
    if month in (6, 7, 8, 9):
        return summer
    if month in (12, 1, 2):
        return winter
    return spring


_ALARM_TYPES = (
    "海上雷雨大风", "雷雨大风", "道路冰雪", "道路结冰", "森林火险", "大风降温",
    "强对流", "冰雹", "台风", "暴雨", "暴雪", "寒潮", "高温", "低温", "雷电",
    "大风", "大雾", "霾", "扬沙", "沙尘暴", "霜冻", "寒潮霜冻", "持续低温",
)


def parse_alarm(text: str) -> tuple[str, str]:
    """从「…气象台发布海上雷雨大风黄色预警信号」里抠出 (类型, 级别)。"""
    level = ""
    m = re.search(r"(红|橙|黄|蓝)色", text)
    if m:
        level = m.group(1)
    seg_m = re.search(r"发布(.+?)预[警警]", text)
    seg = seg_m.group(1) if seg_m else text
    seg = re.sub(r"(红|橙|黄|蓝)色", "", seg)
    for t in _ALARM_TYPES:
        if t in seg:
            return t, level
    return (seg.replace("信号", "").strip()[:8] if seg_m else ""), level


def select_alarms(rows: list[dict], city: CityInfo, source: str) -> list[AlarmRecord]:
    """只保留目标城市/本省范围内的预警，并按严重度倒序。

    绝不做的事：把海南的海上大风预警当成成都的预警展示给游客——
    这类"看起来像数据"的噪声会直接毁掉天气建议的可信度。
    """
    out: list[AlarmRecord] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or r.get("alertid") or r.get("alertidno") or "")
        title = _first_str(r, ("title", "headline", "alertTitle", "name"))
        text = _first_str(r, ("description", "text", "content", "alertDesc"))
        if not (title or text):
            continue
        blob = f"{title} {text}"
        alarm_type, level = parse_alarm(blob)
        level = level or _first_str(r, ("signallevel", "level"))
        in_city = city.name in blob or city.name.rstrip("市") in blob
        in_province = bool(city.adcode) and len(rid) >= 6 and rid[:2] == city.adcode[:2]
        if not (in_city or in_province):
            continue
        out.append(
            AlarmRecord(
                title=title or text[:40],
                alarm_type=alarm_type,
                level=level,
                severity=LEVEL_TO_SEVERITY.get(level, Severity.WATCH),
                issuer=_first_str(r, ("publisher", "sender", "issueby")) or "当地气象台",
                issued_at=_first_str(r, ("effective", "issuetime", "pubTime", "issueTime")),
                area=city.name,
                text=text or title,
                url=_first_str(r, ("url", "link", "href")),
                source=source,
                scope="city" if in_city else "province",
            )
        )
    out.sort(key=lambda a: (a.scope != "city", -a.severity.rank))
    # 同一类型+同一级别只留一条：气象台按县区逐个发布，屏幕上出现 5 条「[蓝]暴雨」
    # 会让人怀疑数据质量，而不是觉得信息量大。
    deduped: list[AlarmRecord] = []
    seen: set[tuple[str, str]] = set()
    for a in out:
        key = (a.alarm_type or a.title[:6], a.level)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)
    return deduped[:8]


def _first_str(row: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""
