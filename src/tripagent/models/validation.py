"""校验报告：把"行程是否合理"从主观感受变成可展示的分数。

三分法（env / commonsense / hard）沿用 TravelPlanner 与 ChinaTravel 的约束分类，
这样向评委解释"通过率"时口径是学界通用的，而不是自定义指标。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .enums import ConstraintKind


class ConstraintCheck(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: ConstraintKind = ConstraintKind.COMMONSENSE
    name: str = ""
    passed: bool = True
    weight: float = 1.0
    detail: str = ""
    fix_hint: str = ""
    related: list[str] = Field(default_factory=list)

    @property
    def icon(self) -> str:
        return "✅" if self.passed else "⚠️"

    def one_line(self) -> str:
        line = f"{self.icon} {self.name}"
        if not self.passed:
            line += f" —— {self.detail}"
            if self.fix_hint:
                line += f"（建议：{self.fix_hint}）"
        return line


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    checks: list[ConstraintCheck] = Field(default_factory=list)

    def add(self, check: ConstraintCheck) -> None:
        self.checks.append(check)

    def _subset(self, kind: ConstraintKind) -> list[ConstraintCheck]:
        return [c for c in self.checks if c.kind == kind]

    def pass_rate(self, kind: ConstraintKind | None = None) -> float:
        subset = self._subset(kind) if kind else self.checks
        if not subset:
            return 1.0
        return sum(1 for c in subset if c.passed) / len(subset)

    @property
    def failed(self) -> list[ConstraintCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def hard_failed(self) -> bool:
        return any(c.kind == ConstraintKind.HARD and not c.passed for c in self.checks)

    @property
    def overall(self) -> float:
        total_w = sum(c.weight for c in self.checks) or 1.0
        return sum(c.weight for c in self.checks if c.passed) / total_w

    def verdict(self) -> str:
        if not self.checks:
            return "未校验"
        if self.hard_failed:
            return "存在硬约束冲突，需要调整"
        rate = self.pass_rate()
        if rate >= 0.95:
            return "方案可直接执行"
        if rate >= 0.8:
            return "基本合理，有少量可优化项"
        return "需人工复核"

    def scorecard(self) -> dict[str, float]:
        return {
            "env": round(self.pass_rate(ConstraintKind.ENV), 3),
            "commonsense": round(self.pass_rate(ConstraintKind.COMMONSENSE), 3),
            "hard": round(self.pass_rate(ConstraintKind.HARD), 3),
            "overall": round(self.overall, 3),
        }
