"""天气风险规则引擎。

这是本项目与"普通行程生成器"最大的差别：天气不只是 UI 上的一个图标，
它会（1）在极端天气时直接给出"不建议出行"结论，（2）在可救的情况下把户外项换成室内项，
（3）在 15 天窗口里替用户挑出最合适的日期。全部为确定性规则，可单测、可解释。
"""

from __future__ import annotations

from datetime import date

from ..models.enums import Severity
from ..models.request import Party
from ..models.weather import AirQuality, AlarmRecord, CityWeather, DayForecast, RiskFactor, WeatherRisk

# 灾害类型 → 该类型下"直接不建议出行"的最低级别
_BLOCKING_ALARMS = {
    "台风": {"橙", "红"},
    "暴雨": {"红"},
    "暴雪": {"红"},
    "道路结冰": {"红"},
    "道路冰雪": {"红"},
    "洪水": {"橙", "红"},
}
_WARN_ALARMS = {"台风", "暴雨", "暴雪", "寒潮", "大风", "雷电", "强对流", "冰雹", "道路结冰", "道路冰雪"}


def comfort_index(f: DayForecast) -> int:
    """出行舒适指数 0-100：温度舒适 + 少雨 + 少风 + 不太湿。"""
    score = 100.0
    tmax, tmin = f.tmax_c, f.tmin_c
    if tmax is not None:
        if tmax >= 40:
            score -= 45
        elif tmax >= 37:
            score -= 30
        elif tmax >= 35:
            score -= 18
        elif tmax >= 30:
            score -= 6
        elif tmax <= -5:
            score -= 30
        elif tmax <= 0:
            score -= 15
        elif tmax <= 8:
            score -= 6
    if tmin is not None and tmax is not None and (tmax - tmin) >= 14:
        score -= 6  # 昼夜温差大，老人小孩易不适
    score -= min(35, f.precip_mm * 2.2)
    if f.precip_prob and f.precip_prob >= 70:
        score -= 12
    if f.wmo_code in (95, 96, 99):
        score -= 22  # 雷暴/冰雹：排队区与露天项目直接不可用
    elif f.wmo_code in (45, 48):
        score -= 10  # 雾：观景与交通受影响
    elif f.is_snow:
        score -= 12
    if f.wind_max_kmh:
        if f.wind_max_kmh >= 60:
            score -= 25
        elif f.wind_max_kmh >= 40:
            score -= 12
        elif f.wind_max_kmh >= 30:
            score -= 5
    if f.humidity_max and f.humidity_max >= 90 and (tmax or 0) >= 28:
        score -= 8  # 闷热
    return int(max(0, min(100, round(score))))


def assess_day(forecast: DayForecast, alarms: list[AlarmRecord], party: Party | None = None,
               air: AirQuality | None = None) -> WeatherRisk:
    """单日风险评估。规则全部展开写，方便二次开发逐条调整阈值。"""
    party = party or Party()
    risk = WeatherRisk(date=forecast.date, severity=Severity.NONE)

    # 空气质量是独立于"天气现象"的一维：晴天静风 + 高 PM2.5 是最容易被漏掉的组合，
    # 只看天气预报会给出"适合全天户外"的结论，而那天跑步其实不该出门。
    if air is not None:
        aqi = air.aqi_us or 0.0
        pm = air.pm25_max or 0.0
        if aqi >= 150 or pm >= 115:
            risk.add(RiskFactor("空气污染", f"AQI {aqi:.0f}、PM2.5 {pm:.0f}µg/m³，敏感人群应避免户外",
                                Severity.WARN, True))
        elif aqi >= 100 or pm >= 75:
            risk.add(RiskFactor("空气质量偏差", f"AQI {aqi:.0f}、PM2.5 {pm:.0f}µg/m³", Severity.WATCH, False))
        if (air.o3_max or 0) >= 160:
            risk.add(RiskFactor("臭氧偏高", f"午后臭氧 {air.o3_max:.0f}µg/m³，有氧运动放清晨更合适",
                                Severity.WATCH, False))

    if forecast.wmo_code in (95, 96, 99):
        risk.add(RiskFactor("雷暴", f"{forecast.text}，户外排队项目与索道/玻璃栈道可能临时停运", Severity.WARN, True))
    elif forecast.is_snow:
        risk.add(RiskFactor("降雪", f"{forecast.text}，步行与山区路段湿滑", Severity.WATCH, False))

    if forecast.precip_mm >= 50:
        risk.add(RiskFactor("暴雨级降水", f"预计日降水量 {forecast.precip_mm:.0f}mm，可能内涝", Severity.DANGER, True))
    elif forecast.precip_mm >= 25:
        risk.add(RiskFactor("大雨", f"预计日降水量 {forecast.precip_mm:.0f}mm", Severity.WARN, True))
    elif forecast.precip_mm >= 10:
        risk.add(RiskFactor("中雨", f"预计日降水量 {forecast.precip_mm:.0f}mm，建议带伞并优先室内", Severity.WATCH, True))
    elif forecast.is_rainy_light:
        risk.add(RiskFactor("小雨", "有零星降水，户外行程可执行但需备雨具", Severity.NONE, False))

    if forecast.tmax_c is not None:
        if forecast.tmax_c >= 40:
            risk.add(RiskFactor("极端高温", f"最高 {forecast.tmax_c:.0f}℃，中暑风险高", Severity.DANGER, True))
        elif forecast.tmax_c >= 37:
            risk.add(RiskFactor("高温", f"最高 {forecast.tmax_c:.0f}℃，11:00-16:00 避免长时间户外", Severity.WARN, True))
        elif forecast.tmax_c >= 35:
            risk.add(RiskFactor("炎热", f"最高 {forecast.tmax_c:.0f}℃", Severity.WATCH, True))
        if forecast.tmax_c <= -5:
            risk.add(RiskFactor("严寒", f"最高仅 {forecast.tmax_c:.0f}℃，注意保暖与设备低温掉电", Severity.WARN, False))

    if forecast.tmin_c is not None and forecast.tmin_c <= 0 and forecast.precip_mm >= 3:
        risk.add(RiskFactor("道路结冰", "路面温度低于 0℃ 且有降水，早晚路段湿滑", Severity.WARN, False))

    if forecast.wind_max_kmh:
        if forecast.wind_max_kmh >= 80:
            risk.add(RiskFactor("烈风", f"阵风可达 {forecast.wind_max_kmh:.0f}km/h", Severity.DANGER, True))
        elif forecast.wind_max_kmh >= 55:
            risk.add(RiskFactor("大风", f"{forecast.wind_max_kmh:.0f}km/h，滨海/高空项目可能停运", Severity.WARN, True))
        elif forecast.wind_max_kmh >= 40:
            risk.add(RiskFactor("风大", f"{forecast.wind_max_kmh:.0f}km/h", Severity.WATCH, False))

    if party.needs_care and forecast.tmax_c and forecast.tmax_c >= 33 and forecast.precip_mm < 1:
        risk.add(RiskFactor("老人/儿童友好度下降", f"同行有长辈或儿童，{forecast.tmax_c:.0f}℃ 高温下建议缩短户外暴晒", Severity.WATCH, True))
    if party.elders > 0 and forecast.tmin_c is not None and forecast.tmin_c <= 5:
        risk.add(RiskFactor("低温保暖", "长辈同行，清晨/夜间户外活动建议推后", Severity.WATCH, False))

    for a in alarms:
        hit = _alarm_text(a)
        if not hit:
            continue
        blocking = _is_blocking(a)
        sev = Severity.DANGER if blocking else (Severity.WARN if a.severity.rank >= Severity.WARN.rank else Severity.WATCH)
        risk.add(RiskFactor(f"气象预警·{a.alarm_type or a.level}", f"{a.title}：{a.text[:80]}", sev, a.alarm_type in _WARN_ALARMS))
        if blocking:
            risk.travel_not_recommended = True

    if risk.severity.rank >= Severity.WARN.rank:
        risk.prefer_indoor = True
    risk.advice = _advice_for(risk, forecast, party)
    return risk


def _alarm_text(a: AlarmRecord) -> str:
    return f"{a.alarm_type}{a.level}".strip()


def _is_blocking(a: AlarmRecord) -> bool:
    types = {a.alarm_type, *(a.alarm_type or "").split("、")}
    for t in types:
        if t in _BLOCKING_ALARMS and a.level in _BLOCKING_ALARMS[t]:
            return True
    return a.level == "红"


def _advice_for(risk: WeatherRisk, f: DayForecast, party: Party) -> list[str]:
    out: list[str] = []
    sev = risk.severity
    if sev.rank == Severity.NONE.rank:
        out.append("适宜全天户外游览")
    if f.is_wet or risk.prefer_indoor:
        out.append("室内优先：把博物馆/展馆/商场/剧场排进下午时段，户外景点只留降水概率低的上午，随身带雨具与干袋")
    if any(x.name.startswith("高温") or x.name.startswith("极端高温") for x in risk.factors):
        out.append("11:00-16:00 安排室内或午休，傍晚再出门")
    if any(x.name.startswith("大风") or x.name.startswith("烈风") for x in risk.factors):
        out.append("索道、游船、玻璃栈道、露天演出需现场确认是否开放")
    if any("结冰" in x.name or x.name.startswith("严寒") for x in risk.factors):
        out.append("鞋子防滑、手机保暖，自驾请确认路况")
    if any(x.name.startswith("空气污染") for x in risk.factors):
        out.append("空气质量差：把长时间步行与有氧换成室内场馆，敏感人群戴 KN95")
    elif any(x.name == "空气质量偏差" for x in risk.factors):
        out.append("空气质量一般，清晨比午后适合户外，久留的地方尽量靠绿化")
    if any(x.name == "臭氧偏高" for x in risk.factors):
        out.append("臭氧午后最高，跑步与骑行安排在清晨，傍晚再散步")
    # 下面三条只靠数值就能判定，以前漏掉会让"冷天"和"热天"给出同一条建议
    if f.tmin_c is not None and f.tmax_c is not None and f.tmax_c - f.tmin_c >= 12:
        out.append(f"昼夜温差 {f.tmax_c - f.tmin_c:.0f}℃，早晚外套、午间单衣，行程按可脱卸穿衣安排")
    if f.uv_index and f.uv_index >= 7:
        out.append("紫外线强，露天排队与观景时段尽量避开 12:00-15:00")
    if f.humidity_max is not None and f.humidity_max <= 30:
        out.append("空气很干，户外时间越长越要主动补水，润唇与保湿随身带")
    if party.kids and sev.rank >= Severity.WARN.rank:
        out.append("带儿童同行：优先选择地铁可达的室内场所，减少露天排队")
    if party.elders and f.tmin_c is not None and f.tmin_c <= 3:
        out.append("长辈同行：清晨最冷，把户外早课改成 9:30 之后出发")
    if risk.travel_not_recommended:
        out.append("该日存在高等级气象预警，建议整体改期或压缩为室内半日游")
    return out


def assess(
    weather: CityWeather,
    window: list[date],
    party: Party | None = None,
) -> dict[str, object]:
    """写入 weather.risks / weather.overall，并返回汇总结论。

    best_dates：在 15 天窗口内按舒适指数挑出的候选出发日，用于"要不要换个日子"的建议。
    """
    party = party or Party()
    risks: dict[str, WeatherRisk] = {}
    alarms_by_day: dict[str, list[AlarmRecord]] = {}
    for a in weather.alarms:
        for key in _alarm_dates(a):
            alarms_by_day.setdefault(key, []).append(a)

    for f in weather.forecast:
        key = f.date.isoformat()
        risks[key] = assess_day(f, alarms_by_day.get(key, []), party, weather.air_on(f.date))
    # 提取不到生效日期的预警，按"窗口期整体生效"处理：宁可保守，也不要漏掉台风/暴雨
    global_alarms = [a for a in weather.alarms if not _alarm_dates(a)]
    if global_alarms:
        targets = list(window) or [f.date for f in weather.forecast[:3]]
        for d in targets:
            key = d.isoformat()
            base, f = risks.get(key), weather.on_day(d)
            if base is None or f is None:
                continue
            for a in global_alarms:
                base.add(
                    RiskFactor(
                        f"气象预警·{a.alarm_type or a.level}",
                        f"{a.title}：{a.text[:80]}",
                        Severity.DANGER if _is_blocking(a) else a.severity,
                        True,
                    )
                )
                if _is_blocking(a):
                    base.travel_not_recommended = True
            base.advice = _advice_for(base, f, party)

    weather.risks = risks
    in_window = [risks[d.isoformat()] for d in window if d.isoformat() in risks]
    overall = Severity.NONE
    for r in in_window:
        if r.severity.rank > overall.rank:
            overall = r.severity
    weather.overall = overall

    comfort = {f.date.isoformat(): comfort_index(f) for f in weather.forecast}
    ranked = sorted(comfort.items(), key=lambda kv: -kv[1])
    best = [k for k, v in ranked[: max(1, len(window)) ] if v >= 60]

    blockers = [r for r in in_window if r.travel_not_recommended or r.severity == Severity.DANGER]
    blocking_advice = ""
    if blockers:
        lines = ["⛔ 不建议按原计划出行："]
        for r in blockers:
            day_txt = r.date.isoformat() if r.date else "窗口期内"
            names = "、".join(x.name for x in r.factors if x.severity.rank >= Severity.WARN.rank) or "极端天气"
            lines.append(f"  · {day_txt}：{names}")
        if best:
            lines.append(f"建议改期窗口：{'、'.join(best[:3])}（这几天舒适指数 ≥60）")
        else:
            lines.append("未来 15 天内暂无明显更优日期，建议改目的地或压缩为室内主题行程")
        blocking_advice = "\n".join(lines)

    return {
        "overall": overall,
        "blocking_advice": blocking_advice,
        "best_dates": best,
        "comfort": comfort,
        "risks": risks,
    }


def _alarm_dates(a: AlarmRecord) -> list[str]:
    """从预警文本里尽力提取生效日期；提取不到就返回空，交给"全局预警"逻辑处理。"""
    import re

    out: list[str] = []
    blob = f"{a.issued_at} {a.text} {a.title}"
    for m in re.finditer(r"(20\d{2})年?[-/年](\d{1,2})月?(\d{1,2})", blob):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat())
        except ValueError:
            continue
    return out


def day_verdict(f: DayForecast, risk: WeatherRisk | None, rank: int = 0, total: int = 0,
                spread: int = 0, air: AirQuality | None = None) -> dict[str, str]:
    """把一天的预报+风险压成一句人能直接决策的话，外加这天的健康提示。

    返回 verdict ∈ {outdoor, indoor, avoid}：
      outdoor = 适合户外为主；indoor = 可以出门但以室内为主；avoid = 不建议出行。

    刻意不做统一模板：用户反馈过"五天五句一模一样，等于没说"。
    advice 由这天的主导要素（温度/风/湿度/紫外线/昼夜温差/降水/雾/雷暴/周末）现场拼；
    tips 讲的是对身体要做什么（保暖、保湿、防晒、防滑），不是行程怎么排。
    """
    from ..models.common import precip_level, temp_span

    wet = precip_level(f.precip_mm, f.precip_prob, f.text)
    sev = risk.severity if risk else Severity.NONE
    verdict = "outdoor"
    if risk and risk.travel_not_recommended:
        verdict = "avoid"
    elif sev.rank >= Severity.WARN.rank or wet in ("大雨", "暴雨"):
        verdict = "indoor" if wet not in ("大雨", "暴雨") else "avoid"
    elif risk and risk.prefer_indoor:
        verdict = "indoor"

    temps = temp_span(f.tmin_c, f.tmax_c)
    names = {x.name for x in (risk.factors if risk else [])}
    if verdict == "avoid":
        advice = _avoid_advice(names)
    elif verdict == "indoor":
        advice = _indoor_advice(f, names, wet)
    else:
        advice = _outdoor_advice(f, wet, rank=rank, total=total, spread=spread)
        astro = _astro_note(f)
        if astro:
            advice += f"；{astro}"
    return {
        "verdict": verdict,
        "date": f.date.strftime("%m/%d"),
        "weekday": "周" + "一二三四五六日"[f.date.weekday()],
        "headline": f"{f.text} {temps}",
        # why 只讲标题里没有的信号。以前 dry 日子会渲染成
        # 「晴 -5~6℃ — 晴，-5~6℃」，同一句话讲两遍，用户直接判定这是模板糊弄。
        "why": "、".join(day_signals(f, wet, air)) or "无附加信号",
        "advice": advice,
        "tips": "；".join(safety_tips(f, risk, air)),
        "comfort": str(comfort_index(f)),
        "severity": sev.label,
    }


def day_signals(f: DayForecast, wet: str = "", air: AirQuality | None = None) -> list[str]:
    """标题之外还能说点什么：风、湿度、紫外线、昼夜差、日落、空气质量、舒适指数。"""
    from ..models.common import precip_level

    wet = wet or precip_level(f.precip_mm, f.precip_prob, f.text)
    out: list[str] = []
    if wet not in ("无雨",):
        out.append(wet)
    if f.wind_max_kmh:
        out.append(f"阵风 {f.wind_max_kmh:.0f}km/h")
    if f.humidity_max is not None:
        out.append(f"湿度 {f.humidity_max:.0f}%")
    if f.uv_index:
        out.append(f"紫外线 {f.uv_index:.0f}")
    if air is not None and air.aqi_us is not None:
        out.append(f"空气质量 {air.level}（AQI {air.aqi_us:.0f}）")
    if f.tmax_c is not None and f.tmin_c is not None and f.tmax_c - f.tmin_c >= 12:
        out.append(f"昼夜差 {f.tmax_c - f.tmin_c:.0f}℃")
    if f.sunset:
        out.append(f"日落 {f.sunset}")
    out.append(f"舒适指数 {comfort_index(f)}")
    return out


def _astro_note(f: DayForecast) -> str:
    """天文窗口：月相决定"今晚能不能看银河"。

    只在数据真的存在时才说话：拿不到月相就一句不提，而不是编一句"月色不错"——
    把天文写成模板，比不写更容易被看穿。
    """
    bits: list[str] = []
    ph = f.moon_phase
    if ph is not None:
        if ph <= 0.15 or ph >= 0.85:
            bits.append("月相近新月，光污染小的地方适合观星与银河长曝")
        elif 0.35 <= ph <= 0.65:
            bits.append("接近满月，月光会盖掉暗星，观星不如拍月景或夜色建筑")
    if f.moonrise:
        bits.append(f"月升 {f.moonrise}")
    return "；".join(bits)


def _avoid_advice(names: set[str]) -> str:
    """不建议出行要说清"为什么别去 + 那这天空出来干什么"，一句"建议改期"太敷衍。"""
    kind = next((x.split("·", 1)[1] for x in names if x.startswith("气象预警")), "")
    if kind:
        return f"{kind}预警已到停出行级别，这天空出来，整体后移或改成酒店与商场半日"
    if any(x.startswith("极端高温") for x in names):
        return "正午已达危险级别高温，这天别安排户外，改室内或只在清晨短走"
    if any("结冰" in x for x in names):
        return "路面结冰风险高，自驾与山区路线这天取消，市区步行也建议推后"
    if any(x.startswith("暴雨") for x in names):
        return "降水已达暴雨量级，内涝与封路风险高，这天留在室内"
    return "该日天气不适合游玩，留在室内休整，把重头戏挪到后面几天"


def _indoor_advice(f: DayForecast, names: set[str], wet: str) -> str:
    lead: list[str] = []
    if wet in ("中雨", "大雨", "暴雨", "可能下雨"):
        lead.append(f"室内为主（{wet}），博物馆、展馆、剧场、商场优先，户外只留降水概率低的上午")
    if "雷暴" in names:
        lead.append("露天排队与索道午后可能停运，演出与展馆排在下午更稳")
    if any(x.startswith("大风") or x.startswith("烈风") for x in names):
        lead.append("高空与水上项目（索道、游船、玻璃栈道）现场确认再走")
    if any(x.startswith("高温") or x.startswith("极端高温") for x in names):
        lead.append("11 点到 16 点交给室内或午休，傍晚再出门")
    if f.wmo_code in (45, 48):
        lead.append("有雾，观景台与摄影点价值下降，改逛古镇、早市、展馆")
    if any(x.startswith("空气污染") for x in names):
        lead.append("空气质量差：博物馆、展馆、商场、影院这类室内场馆优先，"
                    "户外只留短时穿行，别在主干道旁排队等车")
    elif any(x.startswith("空气质量偏差") for x in names):
        lead.append("空气质量一般：户外段挑清晨与雨后，久留的地方靠绿化而不是靠商圈门口")
    if "臭氧偏高" in names:
        lead.append("午后臭氧高，骑行/徒步这类深呼吸的行程挪到清晨")
    if not lead:
        lead.append("以室内为主，户外只留天气最好的两三个小时")
    return "；".join(lead[:2])


def _outdoor_advice(f: DayForecast, wet: str, comfort: int | None = None,
                    rank: int = 0, total: int = 0, spread: int = 0) -> str:
    """适合户外的日子也要说清"这天怎么玩最划算"，按主导要素给不同说法。

    rank/spread 是这天在整段行程里的舒适排名与极差。用户抱怨过"五天建议一模一样"，
    但解法不能是编造区别：五天舒适指数都是 100 时写"这天体感最弱"是撒谎。
    所以只有极差 ≥6 才用排名，其余情况用每天必然不同的事实做锚点（星期与日落时刻）。
    """
    bits: list[str] = []
    tmax, tmin = f.tmax_c, f.tmin_c
    comfort = comfort_index(f) if comfort is None else comfort
    if tmax is not None and tmax <= 5:
        bits.append("冷但能干，山景古镇照玩，把户外重头戏放在 11:00-15:00 最暖的两小时")
    elif tmax is not None and tmax >= 30:
        bits.append("适合户外但偏热，重头戏放 9 点前与 17 点后，正午躲进展馆或树荫")
    elif total >= 2 and spread >= 6 and rank == 1:
        bits.append(f"这 {total} 天里体感第 1 好（舒适指数 {comfort}，其它日子低到 {comfort - spread}），把最想去的重头戏放这天")
    elif total >= 2 and spread >= 6 and rank == total:
        bits.append(f"这 {total} 天里体感最弱（舒适指数 {comfort}），安排轻松的街区与展馆就好")
    elif total >= 2 and spread >= 6:
        bits.append(f"体感排第 {rank}/{total}（舒适指数 {comfort}），适合放次一级的景点与街区漫步")
    elif total >= 2:
        bits.append(f"和这 {total} 天里的其它日子差不多（舒适指数 {comfort}），按你想去的顺序排就行")
    elif comfort >= 80:
        bits.append(f"舒适指数 {comfort}，全天户外没问题，重头戏可以放这天")
    else:
        bits.append(f"舒适指数 {comfort}，适合户外，需要体力的项目放这天，轻松好走的往后排")

    if f.date.weekday() <= 3:
        bits.append("工作日人少，热门馆与网红机位不用抢")
    elif f.date.weekday() == 4:
        bits.append("周五下午起人流开始上来，早去晚归更划算")
    else:
        bits.append("周末人流大，热门馆开门第一批进或提前预约")
    if f.sunset:
        bits.append(f"日落 {f.sunset}，想拍晚霞就把傍晚段卡在它前 40 分钟")
    if tmax is not None and tmin is not None and tmax - tmin >= 12:
        bits.append(f"昼夜温差 {tmax - tmin:.0f}℃，洋葱式穿衣，中午能脱")
    if wet == "小雨":
        bits.append("上午大概率干爽，户外放上午，下午留给咖啡馆与展馆")
    elif wet == "可能下雨":
        bits.append("降水概率不低但雨量小，出发前看一眼两小时预报")
    if f.wind_max_kmh and f.wind_max_kmh >= 30:
        bits.append("风大，湖边海边体感更冷，帽子压好")
    if f.uv_index and f.uv_index >= 7:
        bits.append("紫外线强，阴凉处也照样晒伤，遮阳帽比防晒霜更管用")
    if f.humidity_max and f.humidity_max >= 85 and (tmax or 0) >= 26:
        bits.append("闷热，选靠水或有风的地方，中间多安排能坐下的休息")
    return "；".join(bits[:4])


def safety_tips(f: DayForecast, risk: WeatherRisk | None = None, air: AirQuality | None = None) -> list[str]:
    """这天的健康与安全提醒：只讲对身体要做什么，不讲行程怎么排。"""
    tips: list[str] = []
    tmax, tmin = f.tmax_c, f.tmin_c
    if tmin is not None and tmin <= -5:
        tips.append("严寒：露肤部位保暖，手机贴内侧口袋防低温掉电")
    elif tmin is not None and tmin <= 3:
        tips.append("清晨冷：围巾和帽子比多穿一件更管用")
    if tmax is not None and tmax >= 35:
        tips.append("高温：每小时补水 300ml，含盐饮品优于纯水，头晕立刻进室内")
    elif tmax is not None and tmax >= 30:
        tips.append("天热：随身 1L 水，别等口渴再喝")
    if f.wind_max_kmh and f.wind_max_kmh >= 30:
        tips.append("风大：保湿面霜与润唇膏备好，眼睛干涩可带人工泪液")
    if f.humidity_max is not None and f.humidity_max <= 30:
        tips.append("空气干：多喝水，鼻子干可带生理盐水喷雾")
    if f.uv_index and f.uv_index >= 6:
        tips.append("紫外线强：SPF50 每 2 小时补一次，墨镜护住眼角")
    if f.precip_mm >= 10 or (f.precip_prob or 0) >= 70:
        tips.append("有雨：穿防水防滑的鞋，随身带干袋装湿衣，地铁站口台阶最滑")
    elif f.is_rainy_light:
        tips.append("零星小雨：一把折叠伞就够，户外照玩")
    if f.is_snow or (risk and any("结冰" in x.name for x in risk.factors)):
        tips.append("湿滑：穿齿底鞋，小步走，双手别插兜以便随时平衡")
    if f.wmo_code in (95, 96, 99):
        tips.append("雷暴：远离空旷高处与水边，听到雷声就先进建筑")
    if f.wmo_code in (45, 48):
        tips.append("大雾：能见度低，山区路段缓行，关节注意保暖")
    if risk and any(x.name.startswith(("霾", "扬沙", "沙尘")) for x in risk.factors):
        tips.append("沙尘或霾：戴 KN95，减少长时间户外有氧运动")
    if air is not None:
        aqi = air.aqi_us or 0
        if aqi >= 150:
            tips.append(f"空气质量 {air.level}（AQI {aqi:.0f}）：口罩 + 缩短户外，哮喘/心血管敏感者别安排徒步")
        elif aqi >= 100:
            tips.append(f"空气质量 {air.level}（AQI {aqi:.0f}）：户外有氧放清晨，午后少在主干道旁停留")
        if (air.o3_max or 0) >= 160:
            tips.append("臭氧偏高：跑步骑行改清晨，傍晚臭氧回落后再活动")
    return tips[:4]


def window_verdicts(weather: CityWeather, window: list) -> dict[str, list[dict[str, str]]]:
    """按"能不能户外"把行程日期分成三堆，供界面直接渲染。

    先按舒适指数排一次名：排名是"这段行程内部"的相对事实，
    所以即使五天都是阴天，每天的建议也不会再是同一句话。
    """
    rows = [(d, weather.on_day(d)) for d in window]
    scored = sorted(
        [(d, f) for d, f in rows if f],
        key=lambda x: -comfort_index(x[1]),
    )
    ranks = {d.isoformat(): i + 1 for i, (d, _f) in enumerate(scored)}
    scores = [comfort_index(f) for _d, f in scored]
    spread = (max(scores) - min(scores)) if scores else 0
    groups: dict[str, list[dict[str, str]]] = {"outdoor": [], "indoor": [], "avoid": []}
    missing: list[str] = []
    for d, f in rows:
        if not f:
            missing.append(d.strftime("%m/%d"))
            continue
        v = day_verdict(f, weather.risk_on(d), ranks.get(d.isoformat(), 0), len(scored), spread,
                        weather.air_on(d))
        groups[v["verdict"]].append(v)
    groups["missing"] = missing  # type: ignore[assignment]
    return groups
