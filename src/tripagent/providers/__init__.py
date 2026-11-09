"""provider 注册入口：import 本包即完成所有数据源注册。"""

from __future__ import annotations

from . import base as _base  # noqa: F401  必须先于下面几个模块
from . import culture as _culture  # noqa: F401
from . import evidence as _evidence  # noqa: F401
from . import geo as _geo  # noqa: F401
from . import images as _images  # noqa: F401
from . import llm as _llm  # noqa: F401
from . import lodging as _lodging  # noqa: F401
from . import poi as _poi  # noqa: F401
from . import transit as _transit  # noqa: F401
from . import weather as _weather  # noqa: F401
from .base import Attempt, Cache, Chain, ChainResult, Provider, ProviderError, provider, registry_snapshot

__all__ = [
    "Attempt",
    "Cache",
    "Chain",
    "ChainResult",
    "Provider",
    "ProviderError",
    "provider",
    "registry_snapshot",
]
