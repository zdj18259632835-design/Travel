"""TripAgent —— LifeHack 生活娱乐赛道的旅游规划智能体。

分层（二次开发请按这个边界加代码）：
    knowledge/   静态知识表（城市坐标、天气码、POI 编码）——改数据不改逻辑
    providers/   外部数据源适配层，注册即插即用，失败自动降级
    core/        规则引擎：意图解析、天气风险、行程装配、约束校验
    agents/      编排层：把 providers + core 串成可观测的流水线
    memory/      SQLite 历史与版本，支持"改一行不重算全表"
    export/      Markdown / HTML / .ics 产物
    cli / app    两个运行入口，都只读 TripPlan 契约
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
