"""HTTP 服务层：把引擎暴露成 REST 接口，供 Streamlit 之外的前端与 Qoder Sites 调用。

    .venv\\Scripts\\python.exe -m tripagent serve --port 8000

设计约束（决定了这个文件为什么长这样）：
- 返回体只有 TripPlan/AdvisorAnswer 的 JSON，不重复实现任何业务逻辑；
- 冷城市联网归纳要 1~2 分钟，远超函数计算的常规超时，所以 web_evidence 可关；
- 绝不回显密钥内容，health 只回布尔。
"""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .agents.service import TripService
from .config import get_settings
from .export.markdown import to_markdown
from .knowledge.cities import all_cities
from .models.enums import Category
from .providers.geo import resolve_any


class PlanRequest(BaseModel):
    text: str = Field(min_length=2, max_length=300, description="自然语言诉求")
    web_evidence: bool = Field(default=False, description="是否允许联网口碑归纳（慢，30-90 秒）")


class AskRequest(BaseModel):
    text: str = Field(min_length=2, max_length=200)
    city: str | None = None
    limit: int = Field(default=5, ge=1, le=10)
    web_evidence: bool = Field(
        default=False,
        description="默认关：这是「即时问答」，等 45 秒就不叫即时了。开启后网友反馈来自联网归纳。",
    )


def _answer_dict(answer) -> dict[str, Any]:
    out = asdict(answer)
    for item in out.get("items", []):
        item["poi"] = item["poi"].model_dump(mode="json") if hasattr(item["poi"], "model_dump") else item["poi"]
        item["reviews"] = [r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in item.get("reviews", [])]
    return out


def create_app() -> FastAPI:
    app = FastAPI(title="TripAgent API", version="0.1.0", description="LifeHack 旅游规划智能体 · 引擎 HTTP 层")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # 演示期放开；正式部署应收敛到 Sites 域名
        allow_methods=["*"],
        allow_headers=["*"],
    )
    service = TripService(get_settings())
    token = service.settings.api_token  # 来自 api_keys/tripagent.env，不是系统环境变量

    def guard(x_token: str | None) -> None:
        # 公网暴露时必须有鉴权，否则任何人都能烧你的 DashScope 额度
        if token and x_token != token:
            raise HTTPException(status_code=401, detail="缺少或错误的 X-API-TOKEN")

    @app.get("/api/health")
    def health() -> dict:
        s = service.settings
        return {
            "ok": True,
            "model": s.llm_model,
            "offline": s.offline,
            "keys": {"dashscope": bool(s.dashscope_api_key), "amap": bool(s.amap_key), "qweather": bool(s.qweather_key)},
            "cities_builtin": len(all_cities()),
            "auth_required": bool(token),
        }

    @app.post("/api/plan")
    def plan(req: PlanRequest, x_token: str | None = Header(default=None, alias="X-API-TOKEN")) -> dict:
        guard(x_token)
        prev = service.settings.evidence_chain
        if not req.web_evidence:
            # 函数计算/浏览器端等不了 90 秒：只走本地口碑库，其余数据源照常联网
            service.settings.evidence_chain = "fixture"
        try:
            outcome = service.plan_trip(req.text)
        finally:
            service.settings.evidence_chain = prev
        if outcome.plan is None:
            return {"ok": False, "errors": outcome.errors, "trace": outcome.trace}
        plan = outcome.plan
        sc = plan.scorecard
        return {
            "ok": True,
            # summary 让前端不必自己遍历 checks 才能显示通过率与合计数
            "summary": {
                "destination": plan.request.destination,
                "days": len(plan.days),
                "slot_count": len(plan.all_slots),
                "night_count": sum(1 for s in plan.all_slots if s.type.value in ("夜间娱乐", "观看演出")),
                "indoor_count": sum(1 for s in plan.all_slots if s.indoor),
                "total_cny": round(plan.total_cost_cents / 100, 2),
                "budget_cny": round(plan.request.budget_yuan, 2),
                "pass_rate": round(sc.pass_rate(), 4) if sc else None,
                "dimensions": sc.scorecard() if sc else None,
                "verdict": sc.verdict() if sc else "",
                "failed": [c.name for c in (sc.failed if sc else [])],
                "blocked_by_weather": bool(plan.blocking_advice),
            },
            "plan": plan.model_dump(mode="json"),
            "risk": {
                "overall": str(outcome.risk.get("overall")),
                "blocking_advice": outcome.risk.get("blocking_advice", ""),
                "best_dates": outcome.risk.get("best_dates", []),
            },
            "trace": outcome.trace,
            "filled_by_llm": outcome.filled_by_llm,
            "markdown": to_markdown(plan),
        }

    @app.post("/api/ask")
    def ask(req: AskRequest, x_token: str | None = Header(default=None, alias="X-API-TOKEN")) -> dict:
        guard(x_token)
        prev = service.settings.evidence_chain
        if not req.web_evidence:
            service.settings.evidence_chain = "fixture"
        try:
            answer = service.ask(req.text, city=req.city, limit=req.limit)
        finally:
            service.settings.evidence_chain = prev
        return {"ok": answer.ok, "answer": _answer_dict(answer)}

    @app.get("/api/cities")
    def cities() -> dict:
        return {"cities": [{"key": c.key, "name": c.name, "adcode": c.adcode, "tags": list(c.tags)} for c in all_cities()]}

    @app.get("/api/categories")
    def categories() -> dict:
        return {"categories": [{"value": c.value, "group": c.group} for c in Category]}

    @app.get("/api/weather/{name}")
    def weather(name: str, days: int = 16) -> dict:
        city = resolve_any(name, service.chain)
        if not city:
            raise HTTPException(status_code=404, detail=f"解析不出城市「{name}」")
        weather_obj, sources = service.fetch_weather(city, days)
        from .core import weather_risk

        summary = weather_risk.assess(weather_obj, [f.date for f in weather_obj.forecast])
        return {
            "city": city.name,
            "sources": sources,
            "forecast": [f.model_dump(mode="json") for f in weather_obj.forecast],
            "alarms": [a.model_dump(mode="json") for a in weather_obj.alarms],
            "overall": str(summary.get("overall")),
            "blocking_advice": summary.get("blocking_advice", ""),
            "comfort": summary.get("comfort"),
        }

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    p = argparse.ArgumentParser(prog="tripagent-serve", description="启动 TripAgent HTTP 服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    a = p.parse_args(argv)
    print(f"[i] API 文档 http://{a.host}:{a.port}/docs  ·  健康检查 /api/health")
    if not get_settings().api_token:
        print("[!] 未设置 TRIPAGENT_API_TOKEN：接口无鉴权，只适合本机调试，别直接公网暴露")
    uvicorn.run("tripagent.server:app", host=a.host, port=a.port, reload=a.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
