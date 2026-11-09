"""模型用量台账：公开部署时唯一的止损手段。

为什么必须有它：创空间是公开的，每个访客点一次「开始规划」都会打你的 DashScope Key。
平台侧的额度上限要你去百炼控制台设，但应用侧必须自己有一道闸——
否则一个上午被刷几百次，账单是在你身上。

刻意按 **token 数** 记账而不是按金额：金额依赖模型单价表，单价会变、我们也拿不到实时价，
而 usage 里的 token 数是接口直接给的硬事实。宁可只约束"用量"，也不要编一个可能算错的"花费"。
"""

from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()  # gather() 之后有多个线程同时调模型


class UsageLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}
        if not isinstance(self._data, dict):
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 用临时文件替换：并发写入时不会读到半截 JSON
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass  # 台账写不进去不该让规划失败，但闸值仍按内存里的数字生效

    def day(self, d: date | None = None) -> dict[str, int]:
        return self._data.get((d or date.today()).isoformat(), {})

    def tokens_today(self) -> int:
        row = self.day()
        return int(row.get("prompt", 0)) + int(row.get("completion", 0)) + int(row.get("reasoning", 0))

    def calls_today(self) -> int:
        return int(self.day().get("calls", 0))

    def record(self, prompt: int = 0, completion: int = 0, reasoning: int = 0) -> None:
        key = date.today().isoformat()
        with _LOCK:
            row = self._data.setdefault(key, {"calls": 0, "prompt": 0, "completion": 0, "reasoning": 0})
            row["calls"] = int(row.get("calls", 0)) + 1
            row["prompt"] = int(row.get("prompt", 0)) + int(prompt or 0)
            row["completion"] = int(row.get("completion", 0)) + int(completion or 0)
            row["reasoning"] = int(row.get("reasoning", 0)) + int(reasoning or 0)
            self._save()

    def over_budget(self, limit: int) -> bool:
        """limit<=0 表示不设限。"""
        return limit > 0 and self.tokens_today() >= limit

    def reset(self, d: date | None = None) -> None:
        with _LOCK:
            self._data.pop((d or date.today()).isoformat(), None)
            self._save()


def usage_of(resp: Any) -> tuple[int, int, int]:
    """从 OpenAI 兼容响应里取 (prompt, completion, reasoning)。

    qwen3 的思考 token 藏在 completion_tokens_details 里，
    不单独拆出来的话，你会以为只花了正文那点钱——恰恰是它最贵。
    """
    u = getattr(resp, "usage", None)
    if u is None and isinstance(resp, dict):
        u = resp.get("usage")
        if isinstance(u, dict):
            return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0), int(
                (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
            )
        return 0, 0, 0
    prompt = int(getattr(u, "prompt_tokens", 0) or 0)
    completion = int(getattr(u, "completion_tokens", 0) or 0)
    reasoning = 0
    details = getattr(u, "completion_tokens_details", None)
    if details is not None:
        reasoning = int(getattr(details, "reasoning_tokens", 0) or 0)
    elif isinstance(details, dict):
        reasoning = int(details.get("reasoning_tokens") or 0)
    return prompt, completion, reasoning
