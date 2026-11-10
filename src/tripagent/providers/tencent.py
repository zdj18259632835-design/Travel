"""腾讯位置服务 provider：不做主力数据源，只做**交叉校验**。

为什么不是"再找一个 POI 源"：降级链的语义是"第一个非空即胜出"，
把腾讯排进 poi_chain 里它永远轮不到，那不是校验，那是摆设。

校验的价值在于**分歧可见**：
  两源评分一致 → 这条推荐更可信，置信度小幅上调；
  两源评分差得多 → 不擅自选一个"看起来对的"，而是把两个数字都告诉用户，让他现场核实；
  营业时间冲突 → 直接进避坑提示，因为这是最容易毁掉一天行程的字段。

字段名我不敢打包票：腾讯 WebService 的评分/营业时间字段随 POI 类型与套餐不同，
所以这里对多个可能字段名做兼容取值，并用 scripts/probe_tencent.py 打印原始响应核对一次。
"""

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Any

from ..knowledge.cities import CityInfo
from ..models.poi import POIRecord
from .base import Provider, ProviderError, provider

PLACE_SEARCH = "https://apis.map.qq.com/ws/place/v1/search"

# 配额耗尽后，本次进程内不再撞第二次：status=121 的文案里有"每日调用量"。
# 不这么做的话，每次规划都要白打 8 个请求，既拖慢又可能在平台侧被判异常流量。
_QUOTA_WORDS = ("每日调用量", "上限", "配额", "quota", "rate")
_quota_exhausted = False


def quota_exhausted() -> bool:
    return _quota_exhausted


def _mark_quota_exhausted(message: str) -> None:
    global _quota_exhausted
    if any(w in message for w in _QUOTA_WORDS):
        _quota_exhausted = True


def reset_quota_flag() -> None:
    """测试与"配额隔日恢复"用：进程内那个开关一旦扳下就没人能松开。"""
    global _quota_exhausted
    _quota_exhausted = False


class CallBudget:
    """自己数着次，别等接口告诉你 121。

    个人开发者（未完成企业认证）的腾讯额度是**按接口**给的，量级是个位数到几十次/天。
    公开部署时如果放任每个访客都发 4 次请求，上午就会把当天的量打光，
    而且失败请求在控制台上看着像"异常流量"。所以这里把额度显式分成两份：
      接口告诉我们没额度了（status=121） → 进程内立刻停用；
      我们自己在一天内计数到上限      → 根本不发请求。
    计数落盘（不是内存变量）：创空间会重启，多个 worker 也各有进程，
    内存计数等于没计。
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.path: Path = settings.data_dir / "tencent_budget.json"

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass  # 记不上账不能把规划搞崩，但内存里的闸门照旧生效

    def used_today(self) -> int:
        return int(self._load().get(date.today().isoformat(), 0) or 0)

    def limit(self) -> int:
        """≤0 表示不设自限（只依赖接口回报的 121）。"""
        return int(self.settings.tencent_daily_calls or 0)

    def remaining(self) -> int:
        cap = self.limit()
        if cap <= 0:
            return 999
        return max(0, cap - self.used_today())

    def add(self, n: int = 1) -> int:
        key = date.today().isoformat()
        with _LOCK:
            data = {k: v for k, v in self._load().items() if k == key}  # 只留今天
            data[key] = int(data.get(key, 0) or 0) + n
            self._save(data)
            return data[key]


_LOCK = threading.Lock()  # gather/advisor 会并发进入这里

# 评分可能出现的字段（不同套餐/类型给的不一样）
_RATING_KEYS = ("rating", "comment_score", "star", "overall_rating")
# 营业时间可能出现的字段
_OPEN_KEYS = ("open_time", "opentime", "business_hours", "opening_hours")
# 人均可能出现的字段
_COST_KEYS = ("average_price", "avg_price", "price", "cost")


def _first_of(row: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = row.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def _to_float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _normalize(title: str) -> str:
    """店名匹配用：去掉括号里的分店说明与空白。

    "大龙燚火锅（玉林店）" 与 "大龙燚火锅(玉林店)" 必须认成同一家，
    否则校验会退化成"永远查不到对应记录"。
    """
    import re

    s = re.sub(r"[（(].*?[)）]", "", title or "")
    return re.sub(r"\s+", "", s).lower()


def pick_best(rows: list[dict], name: str) -> dict | None:
    """在搜索结果里挑最可能是同一家的那条。"""
    want = _normalize(name)
    if not want:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _normalize(str(row.get("title") or row.get("name") or ""))
        if title == want:
            return row
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _normalize(str(row.get("title") or row.get("name") or ""))
        if title and (want in title or title in want):
            return row
    return None


def _search_raw(settings, city: CityInfo, name: str, page_size: int = 5) -> dict | None:
    """发一次原始请求，返回解析后的 JSON（探针与 provider 共用同一条路径）。

    返回 None 表示请求本身失败；调用方据此区分"没查到"和"没连上"。
    """
    import httpx

    params = {
        "keyword": name,
        "boundary": f"region({city.adcode or city.name},0)",
        "key": settings.tencent_map_key,
        "page_size": page_size,
        "page_index": 1,
    }
    try:
        r = httpx.get(PLACE_SEARCH, params=params, timeout=10.0,
                      headers={"User-Agent": settings.http_user_agent})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


@provider("poi_crosscheck", "tencent")
class TencentCrossCheck(Provider):
    def available(self) -> bool:
        return bool(self.settings.tencent_map_key) and self.settings.online

    def describe(self) -> tuple[bool, str]:
        if not self.settings.tencent_map_key:
            return False, "未配置 TENCENT_MAP_KEY，跳过双源校验"
        b = self.budget
        used, cap = b.used_today(), b.limit()
        left = f"，今日预算已用 {used}/{cap}" if cap > 0 else ""
        return True, f"腾讯位置服务 Web 服务 Key（与高德交叉校验评分/营业时间/人均）{left}"

    @property
    def budget(self) -> CallBudget:
        if getattr(self, "_budget", None) is None:
            self._budget = CallBudget(self.settings)
        return self._budget

    def _search(self, city: CityInfo, name: str) -> dict:
        def fetch() -> dict:
            # 计数放在"真的发请求"这一刻，不是"打算查几家"：
            # 命中缓存的那几次没花你的额度，记了就是虚报。
            self.budget.add()
            data = _search_raw(self.settings, city, name)
            if data is None:
                raise ProviderError("腾讯无响应或网络失败")
            if int(data.get("status", -1)) != 0:
                msg = f"腾讯返回 status={data.get('status')} {str(data.get('message'))[:80]}"
                _mark_quota_exhausted(msg)
                raise ProviderError(msg)
            best = pick_best(data.get("data") or [], name)
            if not best:
                return {}
            return {
                "title": str(best.get("title") or best.get("name") or "")[:60],
                "address": str(best.get("address") or "")[:120],
                "rating": _to_float(_first_of(best, _RATING_KEYS) or None),
                "open_time": _first_of(best, _OPEN_KEYS)[:60],
                "cost": _to_float(_first_of(best, _COST_KEYS) or None),
                "category": str(best.get("category") or "")[:20],
            }

        return self.cached_call(("poi_crosscheck", "tencent", city.key, name), fetch)

    def check(self, city: CityInfo, names: list[str], limit: int = 4) -> dict[str, dict]:
        """逐个回查。配额有限，默认只校验最多 4 家——真正会被用户看到的就是这些。

        "一家都没查到"要分两种情况说：全都请求失败（Key 错、配额尽、字段变了）
        还是确实没有同名地点。前者必须抛出去，否则站长会以为校验在跑。
        """
        out: dict[str, dict] = {}
        if _quota_exhausted:
            raise ProviderError("腾讯配额今日已耗尽，本次跳过双源校验")
        todo = [n for n in dict.fromkeys(names) if n and n.strip()][:limit]
        left = self.budget.remaining()
        if left <= 0:
            cap = self.budget.limit()
            raise ProviderError(
                f"腾讯调用预算今日已用完（{self.budget.used_today()}/{cap}），跳过双源校验。"
                f"想多校验把 TRIPAGENT_TENCENT_DAILY_CALLS 调大或等次日恢复。"
            )
        if left < len(todo):
            todo = todo[:left]   # 预算不够就少查几家，而不是让整段校验失败
        errors: list[str] = []
        for name in todo:
            try:
                got = self._search(city, name)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {str(exc)[:100]}")
                continue          # 单个查不到不影响主流程，也不假装校验过
            if got:
                out[name] = got
        if not out and errors and len(errors) == len(todo):
            raise ProviderError(f"{len(errors)} 次查询全部失败，例如：{errors[0]}")
        return out


@provider("poi_crosscheck", "none")
class NoCrossCheck(Provider):
    """没配 Key / 离线时的空实现：让上层明确知道"这次没有交叉校验"。"""

    def available(self) -> bool:
        return True

    def check(self, city: CityInfo, names: list[str], limit: int = 4) -> dict[str, dict]:
        return {}
