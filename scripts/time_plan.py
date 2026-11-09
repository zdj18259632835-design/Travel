"""分段计时：一条行程到底慢在哪一段，跑一次就看得见。

用法（项目根目录）：
    .venv\\Scripts\\python.exe scripts\\time_plan.py "阿勒泰 5天 11月20日出发 情侣"
    .venv\\Scripts\\python.exe scripts\\time_plan.py "成都 3天" --repeat 2

为什么需要它：没有分段数据就没有"优化"，只有"猜"。
并发化改了哪一段、还剩哪一段是瓶颈，全靠这里读出来。
--repeat 2 的第二次会命中 6 小时磁盘缓存，两次之差就是"联网检索的真实代价"。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tripagent.agents.service import TripService  # noqa: E402
from tripagent.config import reload_settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="TripAgent 分段计时")
    ap.add_argument("text", help="自然语言诉求，例如「阿勒泰 5天 11月20日出发 情侣」")
    ap.add_argument("--repeat", type=int, default=1, help="跑几次（第二次起命中缓存）")
    args = ap.parse_args()

    svc = TripService(reload_settings())
    s = svc.settings
    print(f"模型={s.llm_model}  离线={not s.online}  缓存={s.cache_ttl_hours}h  "
          f"千问={'有key' if s.dashscope_api_key else '无key'}  高德={'有key' if s.amap_key else '无key'}")

    for i in range(max(1, args.repeat)):
        t0 = time.time()
        outcome = svc.plan_trip(args.text)
        wall = time.time() - t0
        slots = len(outcome.plan.all_slots) if outcome.plan else 0
        print(f"\n—— 第 {i + 1} 次：总耗时 {wall:.1f}s，安排 {slots} 项 ——")
        for k, v in outcome.timings.items():
            if k == "总计":
                continue
            bar = "█" * max(1, int(v / max(1.0, wall) * 40))
            print(f"  {k:<16}{v:>7.1f}s  {bar}")
        print(f"  {'总计':<16}{outcome.timings.get('总计', wall):>7.1f}s")
        for line in outcome.trace:
            if line.startswith(("实拍图", "  最晚完成")):
                print(f"  · {line.strip()}")
        if outcome.errors:
            for e in outcome.errors:
                print(f"  ⚠️ {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
