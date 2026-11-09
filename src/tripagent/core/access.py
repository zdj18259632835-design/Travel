"""访问闸门：把"谁能用"和"用到多少就停"变成两个可测的纯函数。

为什么用口令而不是 IP 白名单：创空间的访问者经过平台代理，源 IP 不可靠，
而比赛场景本来就是"把口令私发给主办方"。口令为空时闸门完全不启用，
所以本地开发和断网演示不受任何影响。

两条独立规则，别混在一起想：
  身份 —— 没有有效口令的人，连表单都看不到；
  额度 —— 到量之后停的是"会花钱的功能"（联网规划），
          不花钱的功能（天气速查）继续开着，页面不至于变成一块死板。
"""

from __future__ import annotations

from dataclasses import dataclass

GUEST = "guest"
VISITOR = "visitor"
OWNER = "owner"


@dataclass(frozen=True)
class Gate:
    enabled: bool          # 是否启用口令闸门（配置了任一口令就启用）
    who: str               # guest / visitor / owner
    allowed: bool          # 能否进入并使用
    reason: str            # 不允许时给人看的原因
    costly_allowed: bool   # 是否允许继续调用会花钱的模型功能


def _split(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def decide(access_codes: str, owner_code: str, budget: int, used_tokens: int,
           submitted: str = "") -> Gate:
    codes = _split(access_codes)
    owner = (owner_code or "").strip()
    enabled = bool(codes or owner)
    submitted = (submitted or "").strip()

    if not enabled:
        return Gate(False, GUEST, True, "", True)

    who = GUEST
    if owner and submitted == owner:
        who = OWNER
    elif codes and submitted in codes:
        who = VISITOR

    if who is GUEST:
        return Gate(True, who, False, "本作品为比赛演示，需要体验口令才能使用。", False)

    over = budget > 0 and used_tokens >= budget
    if over and who is not OWNER:
        return Gate(
            True, who, True,
            f"今日模型额度已用完（{used_tokens:,}/{budget:,} tokens），联网规划已暂停，"
            f"天气速查仍可正常使用；明天自动恢复。",
            False,
        )
    return Gate(True, who, True, "", True)
