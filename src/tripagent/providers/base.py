"""Provider 框架：注册表 + 降级链 + 磁盘缓存。

新增一个数据源的标准动作（二次开发最常用的扩展点）：
1. 写一个类，实现对应能力的方法（如 WeatherSource.fetch_forecast）；
2. 用 @provider("forecast", "my_source") 注册；
3. 把 "my_source" 加到 .env 或 config 的 forecast_chain 里。
引擎、UI、导出层都不需要改动。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Type, TypeVar

import httpx
from pydantic import BaseModel

from ..config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)


class ProviderError(RuntimeError):
    """provider 内部可预期的失败（网络、配额、解析），交由降级链继续尝试。"""


@dataclass
class Attempt:
    capability: str
    provider: str
    ok: bool
    hits: int = 0
    error: str = ""
    ms: int = 0

    def __str__(self) -> str:  # pragma: no cover
        flag = "✓" if self.ok else "✗"
        return f"[{flag} {self.capability}:{self.provider}] hits={self.hits} {self.ms}ms {self.error}".strip()


@dataclass
class ChainResult:
    value: Any = None
    source: str = "none"
    attempts: list[Attempt] = field(default_factory=list)

    def trace(self) -> str:
        return " → ".join(str(a) for a in self.attempts)


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
class Cache:
    def __init__(self, settings: Settings) -> None:
        self.dir = settings.cache_dir
        self.ttl_s = settings.cache_ttl_hours * 3600

    @staticmethod
    def key(*parts: Any) -> str:
        payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def path(self, key: str):
        return self.dir / f"{key}.json"

    def get(self, key: str) -> Any | None:
        p = self.path(key)
        if not p.exists():
            return None
        if self.ttl_s <= 0:
            return None
        if time.time() - p.stat().st_mtime > self.ttl_s:
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def set(self, key: str, value: Any) -> None:
        try:
            self.path(key).write_text(
                json.dumps(value, ensure_ascii=False, default=str), encoding="utf-8"
            )
        except Exception:
            pass  # 缓存永远不该让主流程崩


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------
_REGISTRY: dict[str, dict[str, Type["Provider"]]] = {}


class Provider:
    """所有 provider 的基类：能力名 + 可实例化 + 可用性自检。"""

    capability = "generic"

    def __init__(self, settings: Settings, cache: Cache) -> None:
        self.settings = settings
        self.cache = cache

    def available(self) -> bool:
        return True

    def describe(self) -> tuple[bool, str]:
        """供 `tripagent doctor` 展示：是否可用 + 一句话说明。"""
        return self.available(), ""

    # ---- 子类常用工具 ----
    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        h = {"User-Agent": self.settings.http_user_agent, **(headers or {})}
        try:
            r = httpx.get(
                url,
                params=params,
                headers=h,
                timeout=self.settings.http_timeout,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"网络失败 {url}: {exc}") from exc
        if r.status_code != 200:
            raise ProviderError(f"HTTP {r.status_code} {url}")
        try:
            return r.json()
        except Exception as exc:
            raise ProviderError(f"非 JSON 响应 {url}: {r.text[:80]}") from exc

    def cached_call(self, key_parts: tuple, fetch: Callable[[], Any], model: Type[T] | None = None) -> Any:
        """读缓存 → 未命中则取数 → 落盘 → 反序列化为模型列表/对象。"""
        ck = self.cache.key(*key_parts)
        raw = self.cache.get(ck)
        if raw is not None:
            return self._revive(raw, model)
        value = fetch()
        try:
            self.cache.set(ck, self._dump(value))
        except Exception:
            pass
        return value

    @staticmethod
    def _dump(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return {"__single__": value.model_dump(mode="json")}
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            items = list(value)
            return [i.model_dump(mode="json") if isinstance(i, BaseModel) else i for i in items]
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        return value

    @staticmethod
    def _revive(raw: Any, model: Type[T] | None) -> Any:
        if model is None or raw is None:
            return raw
        if isinstance(raw, dict) and "__single__" in raw:
            return model.model_validate(raw["__single__"])
        if isinstance(raw, list):
            return [model.model_validate(i) for i in raw]
        return model.model_validate(raw)


def provider(capability: str, name: str = ""):
    """装饰器：@provider("forecast", "open_meteo")"""

    def deco(cls: Type[Provider]) -> Type[Provider]:
        cls.capability = capability
        key = name or cls.__name__.lower()
        _REGISTRY.setdefault(capability, {})[key] = cls
        return cls

    return deco


def registry_snapshot() -> dict[str, list[str]]:
    return {k: sorted(v) for k, v in _REGISTRY.items()}


class Chain:
    """按 settings.<capability>_chain 的顺序尝试 provider，直到拿到非空结果。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cache = Cache(self.settings)
        self._instances: dict[str, Provider] = {}
        self._lock = threading.Lock()  # gather() 会多线程共享同一个 Chain
        self.log: list[Attempt] = []

    def providers_for(self, capability: str) -> list[tuple[str, Provider]]:
        out = []
        for name in self.settings.chain(capability):
            cls = _REGISTRY.get(capability, {}).get(name)
            if cls is None:
                self.log.append(Attempt(capability, name, False, error="未注册的 provider 名"))
                continue
            cache_key = f"{capability}:{name}"
            inst = self._instances.get(cache_key)
            if inst is None:
                with self._lock:
                    inst = self._instances.get(cache_key)
                    if inst is None:
                        try:
                            inst = cls(self.settings, self.cache)
                        except Exception as exc:  # 构造失败（如缺 key）直接跳过
                            self.log.append(Attempt(capability, name, False, error=f"初始化失败 {exc}"))
                            continue
                        self._instances[cache_key] = inst
            out.append((name, inst))
        return out

    def run(
        self,
        capability: str,
        method: str,
        *args: Any,
        model: Type[T] | None = None,
        allow_empty: bool = False,
        **kwargs: Any,
    ) -> ChainResult:
        """按链路顺序取数。

        allow_empty=True 用于「空值本身就是有效答案」的能力（例如某城市当前无预警）：
        第一个不抛异常的 provider 直接胜出。否则空列表会被当成失败继续降级，
        让 fixture 把"没有预警"误补成"随机造一条预警"。
        """
        result = ChainResult()
        for name, inst in self.providers_for(capability):
            started = time.time()
            if not inst.available():
                result.attempts.append(Attempt(capability, name, False, error="不可用（缺 key 或离线）"))
                continue
            fn = getattr(inst, method, None)
            if fn is None:
                result.attempts.append(Attempt(capability, name, False, error=f"未实现 {method}"))
                continue
            try:
                value = fn(*args, **kwargs)
            except ProviderError as exc:
                result.attempts.append(Attempt(capability, name, False, error=str(exc)[:160], ms=int((time.time() - started) * 1000)))
                continue
            except Exception as exc:  # 三方 SDK/解析异常不应打断整条链
                result.attempts.append(Attempt(capability, name, False, error=f"{type(exc).__name__}: {exc}"[:160], ms=int((time.time() - started) * 1000)))
                continue
            hits = len(value) if isinstance(value, list) else (1 if value else 0)
            ms = int((time.time() - started) * 1000)
            result.attempts.append(Attempt(capability, name, hits > 0, hits=hits, ms=ms))
            if hits or allow_empty:
                result.value, result.source = value, name
                self.log.extend(result.attempts)
                return result
        self.log.extend(result.attempts)
        return result

    def trace_lines(self) -> list[str]:
        return [str(a) for a in self.log]
