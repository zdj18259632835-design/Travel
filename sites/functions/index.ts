/**
 * TripAgent · Qoder Sites 边缘函数（Deno / runtime=edge）
 *
 * 这个函数刻意"不依赖任何密钥"就能跑：天气数值来自 Open-Meteo（免 key），
 * 灾害预警来自中国气象局公开接口（免 key，但要带 User-Agent，否则 403）。
 * 这样站点发布后立刻可用，不需要先配 Secret 才能看到东西。
 *
 * 规则与 Python 引擎保持一致，改阈值时两边一起改：
 *   src/tripagent/core/weather_risk.py   舒适指数 / verdict / advice / safety_tips
 *   src/tripagent/knowledge/wmo.py       WMO 天气码中文映射
 *   src/tripagent/providers/weather.py   预警类型与级别解析
 *
 * 如果配置了 TRIPAGENT_API_BASE + TRIPAGENT_API_TOKEN（完整 Python 应用地址），
 * action=plan / ask 会转发过去；没配置时明确返回 501，而不是假装成功。
 */

type Day = {
  date: string;
  weekday: string;
  text: string;
  tmin: number | null;
  tmax: number | null;
  precip: number;
  prob: number | null;
  wind: number | null;
  humidity: number | null;
  uv: number | null;
  code: number | null;
  sunset?: string;
  comfort: number;
  verdict: "outdoor" | "indoor" | "avoid";
  advice: string;
  tips: string[];
  signals?: string[];
};

// WMO 码 → [中文, 标记]。w=湿 s=雪 t=雷暴 f=雾
const WMO: Record<number, [string, string]> = {
  0: ["晴", ""], 1: ["基本晴朗", ""], 2: ["局部多云", ""], 3: ["阴", ""],
  45: ["雾", "f"], 48: ["雾凇雾", "f"],
  51: ["小毛毛雨", "w"], 53: ["毛毛雨", "w"], 55: ["大毛毛雨", "w"],
  61: ["小雨", "w"], 63: ["中雨", "w"], 65: ["大雨", "w"],
  66: ["冻雨", "w"], 67: ["强冻雨", "w"],
  71: ["小雪", "ws"], 73: ["中雪", "ws"], 75: ["大雪", "ws"], 77: ["雪粒", "s"],
  80: ["小阵雨", "w"], 81: ["阵雨", "w"], 82: ["强阵雨", "w"],
  85: ["小阵雪", "ws"], 86: ["大阵雪", "ws"],
  95: ["雷暴", "wt"], 96: ["雷暴伴小冰雹", "wt"], 99: ["雷暴伴大冰雹", "wt"],
};

const ALARM_TYPES = [
  "海上雷雨大风", "雷雨大风", "道路冰雪", "道路结冰", "森林火险", "大风降温",
  "强对流", "冰雹", "台风", "暴雨", "暴雪", "寒潮", "高温", "低温", "雷电",
  "大风", "大雾", "霾", "扬沙", "沙尘暴", "霜冻", "持续低温",
];
// 灾害类型 → 直接不建议出行的最低级别
const BLOCKING: Record<string, string[]> = {
  台风: ["橙", "红"], 暴雨: ["红"], 暴雪: ["红"], 道路结冰: ["红"],
  道路冰雪: ["红"], 洪水: ["橙", "红"],
};
const LEVEL_RANK: Record<string, number> = { 蓝: 1, 黄: 2, 橙: 3, 红: 4 };

const num = (v: unknown): number | null => {
  const n = typeof v === "string" ? Number(v) : v;
  return typeof n === "number" && isFinite(n) ? n : null;
};

const weekday = (iso: string): string => {
  const d = new Date(`${iso}T00:00:00+08:00`);
  return "周" + "一二三四五六日"[d.getUTCDay() === 0 ? 6 : d.getUTCDay() - 1];
};

function precipLevel(mm: number, prob: number | null, text: string): string {
  if (mm >= 50) return "暴雨";
  if (mm >= 25) return "大雨";
  if (mm >= 10) return "中雨";
  if (mm >= 0.1) return "小雨";
  if (prob && prob >= 60) return "可能下雨";
  if (/[雨雪雹雷]/.test(text)) return "有雨";
  return "无雨";
}

function tempSpan(tmin: number | null, tmax: number | null): string {
  if (tmin === null || tmax === null) return "气温未知";
  return `${Math.round(tmin)}~${Math.round(tmax)}℃`;
}

/** 与 Python comfort_index 同阈值：温度舒适 + 少雨 + 少风 + 不太湿 */
function comfortIndex(f: Day): number {
  let s = 100;
  const { tmax, tmin } = f;
  if (tmax !== null) {
    if (tmax >= 40) s -= 45;
    else if (tmax >= 37) s -= 30;
    else if (tmax >= 35) s -= 18;
    else if (tmax >= 30) s -= 6;
    else if (tmax <= -5) s -= 30;
    else if (tmax <= 0) s -= 15;
    else if (tmax <= 8) s -= 6;
  }
  if (tmax !== null && tmin !== null && tmax - tmin >= 14) s -= 6;
  s -= Math.min(35, f.precip * 2.2);
  if (f.prob && f.prob >= 70) s -= 12;
  if (f.code === 95 || f.code === 96 || f.code === 99) s -= 22;
  else if (f.code === 45 || f.code === 48) s -= 10;
  else if ((WMO[f.code ?? -1]?.[1] ?? "").includes("s")) s -= 12;
  if (f.wind) {
    if (f.wind >= 60) s -= 25;
    else if (f.wind >= 40) s -= 12;
    else if (f.wind >= 30) s -= 5;
  }
  if (f.humidity && f.humidity >= 90 && (f.tmax ?? 0) >= 28) s -= 8;
  return Math.max(0, Math.min(100, Math.round(s)));
}

function outdoorAdvice(f: Day, wet: string, rank = 0, total = 0, spread = 0): string {
  const bits: string[] = [];
  const { tmax, tmin } = f;
  if (tmax !== null && tmax <= 5) {
    bits.push("冷但能干，山景古镇照玩，把户外重头戏放在 11:00-15:00 最暖的两小时");
  } else if (tmax !== null && tmax >= 30) {
    bits.push("适合户外但偏热，重头戏放 9 点前与 17 点后，正午躲进展馆或树荫");
  } else if (total >= 2 && spread >= 6 && rank === 1) {
    bits.push(`这 ${total} 天里体感第 1 好（舒适指数 ${f.comfort}），把最想去的重头戏放这天`);
  } else if (total >= 2 && spread >= 6 && rank === total) {
    bits.push(`这 ${total} 天里体感最弱（舒适指数 ${f.comfort}），安排轻松的街区与展馆就好`);
  } else if (total >= 2 && spread >= 6) {
    bits.push(`体感排第 ${rank}/${total}（舒适指数 ${f.comfort}），适合放次一级的景点与街区漫步`);
  } else if (total >= 2) {
    bits.push(`和这 ${total} 天里的其它日子差不多（舒适指数 ${f.comfort}），按你想去的顺序排就行`);
  } else if (f.comfort >= 80) {
    bits.push(`舒适指数 ${f.comfort}，全天户外没问题，重头戏可以放这天`);
  } else {
    bits.push(`舒适指数 ${f.comfort}，适合户外，需要体力的项目放这天，轻松好走的往后排`);
  }

  const dow = new Date(`${f.date}T00:00:00+08:00`).getUTCDay();
  if (dow >= 1 && dow <= 4) bits.push("工作日人少，热门馆与网红机位不用抢");
  else if (dow === 5) bits.push("周五下午起人流开始上来，早去晚归更划算");
  else bits.push("周末人流大，热门馆开门第一批进或提前预约");

  if (f.sunset) bits.push(`日落 ${f.sunset}，想拍晚霞就把傍晚段卡在它前 40 分钟`);
  if (tmax !== null && tmin !== null && tmax - tmin >= 12) {
    bits.push(`昼夜温差 ${Math.round(tmax - tmin)}℃，洋葱式穿衣，中午能脱`);
  }
  if (wet === "小雨") bits.push("上午大概率干爽，户外放上午，下午留给咖啡馆与展馆");
  else if (wet === "可能下雨") bits.push("降水概率不低但雨量小，出发前看一眼两小时预报");
  if (f.wind && f.wind >= 30) bits.push("风大，湖边海边体感更冷，帽子压好");
  if (f.uv && f.uv >= 7) bits.push("紫外线强，阴凉处也照样晒伤，遮阳帽比防晒霜更管用");
  if (f.humidity && f.humidity >= 85 && (f.tmax ?? 0) >= 26) {
    bits.push("闷热，选靠水或有风的地方，中间多安排能坐下的休息");
  }
  return bits.slice(0, 4).join("；");
}

function indoorAdvice(f: Day, wet: string): string {
  const lead: string[] = [];
  if (["中雨", "大雨", "暴雨", "可能下雨"].includes(wet)) {
    lead.push(`室内为主（${wet}），博物馆、展馆、剧场、商场优先，户外只留降水概率低的上午`);
  }
  if (f.code === 95 || f.code === 96 || f.code === 99) {
    lead.push("露天排队与索道午后可能停运，演出与展馆排在下午更稳");
  }
  if (f.wind && f.wind >= 55) lead.push("高空与水上项目（索道、游船、玻璃栈道）现场确认再走");
  if (f.tmax !== null && f.tmax >= 37) lead.push("11 点到 16 点交给室内或午休，傍晚再出门");
  if (f.code === 45 || f.code === 48) lead.push("有雾，观景台与摄影点价值下降，改逛古镇、早市、展馆");
  if (!lead.length) lead.push("以室内为主，户外只留天气最好的两三个小时");
  return lead.slice(0, 2).join("；");
}

function safetyTips(f: Day): string[] {
  const tips: string[] = [];
  const { tmax, tmin } = f;
  if (tmin !== null && tmin <= -5) tips.push("严寒：露肤部位保暖，手机贴内侧口袋防低温掉电");
  else if (tmin !== null && tmin <= 3) tips.push("清晨冷：围巾和帽子比多穿一件更管用");
  if (tmax !== null && tmax >= 35) tips.push("高温：每小时补水 300ml，含盐饮品优于纯水，头晕立刻进室内");
  else if (tmax !== null && tmax >= 30) tips.push("天热：随身 1L 水，别等口渴再喝");
  if (f.wind && f.wind >= 30) tips.push("风大：保湿面霜与润唇膏备好，眼睛干涩可带人工泪液");
  if (f.humidity !== null && f.humidity <= 30) tips.push("空气干：多喝水，鼻子干可带生理盐水喷雾");
  if (f.uv && f.uv >= 6) tips.push("紫外线强：SPF50 每 2 小时补一次，墨镜护住眼角");
  if (f.precip >= 10 || (f.prob ?? 0) >= 70) tips.push("有雨：穿防水防滑的鞋，随身带干袋装湿衣，地铁站口台阶最滑");
  else if (f.precip >= 0.1) tips.push("零星小雨：一把折叠伞就够，户外照玩");
  const flags = WMO[f.code ?? -1]?.[1] ?? "";
  if (flags.includes("s")) tips.push("湿滑：穿齿底鞋，小步走，双手别插兜以便随时平衡");
  if (flags.includes("t")) tips.push("雷暴：远离空旷高处与水边，听到雷声就先进建筑");
  if (flags.includes("f")) tips.push("大雾：能见度低，山区路段缓行，关节注意保暖");
  return tips.slice(0, 4);
}

function parseAlarm(text: string): { type: string; level: string } {
  const lm = text.match(/(红|橙|黄|蓝)色/);
  const level = lm ? lm[1] : "";
  const sm = text.match(/发布(.+?)预[警警]/);
  let seg = sm ? sm[1] : text;
  seg = seg.replace(/(红|橙|黄|蓝)色/g, "");
  for (const t of ALARM_TYPES) if (seg.includes(t)) return { type: t, level };
  return { type: sm ? seg.replace("信号", "").trim().slice(0, 8) : "", level };
}

async function fetchJson(url: string, init?: RequestInit, ms = 15000): Promise<any> {
  const res = await fetch(url, { ...init, signal: AbortSignal.timeout(ms) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}

/** 中国气象局公开预警：按 adcode 前缀 + 地名双重过滤，同类型同级别只留一条 */
async function cmaAlarms(city: string, province: string, adcode: string): Promise<any[]> {
  let rows: any[] = [];
  try {
    const data = await fetchJson("https://weather.cma.cn/api/alarm", {
      headers: { Accept: "application/json", "User-Agent": "Mozilla/5.0 (compatible; TripAgent/0.1)" },
    }, 20000);
    rows = Array.isArray(data?.data) ? data.data : [];
  } catch {
    return []; // 预警拿不到不该让天气结论整体失败
  }
  const short = city.replace(/[市县区旗]$/, "");
  const out: any[] = [];
  for (const r of rows) {
    const title = String(r.title || r.headline || "");
    const desc = String(r.description || r.text || "");
    const blob = `${title} ${desc}`;
    if (!title && !desc) continue;
    const rid = String(r.id || "");
    const byCode = Boolean(adcode) && rid.length >= 6 && rid.slice(0, 2) === adcode.slice(0, 2);
    const byName = blob.includes(city) || blob.includes(short) || (province && blob.includes(province));
    if (!(byCode || byName)) continue;
    const { type, level } = parseAlarm(blob);
    out.push({
      title: title || desc.slice(0, 40),
      type: type || "气象预警",
      level,
      desc: desc.slice(0, 160),
      scope: blob.includes(city) || blob.includes(short) ? "city" : "province",
    });
  }
  out.sort((a, b) => (LEVEL_RANK[b.level] ?? 0) - (LEVEL_RANK[a.level] ?? 0));
  const seen = new Set<string>();
  const deduped: any[] = [];
  for (const a of out) {
    const k = `${a.type}|${a.level}`;
    if (seen.has(k)) continue;
    seen.add(k);
    deduped.push(a);
  }
  return deduped.slice(0, 8);
}

import { CITIES } from "./cities.ts";

/** 地名解析：先查内建的全国城市表，再退回 Open-Meteo 拉丁字母检索。
 *
 * 为什么必须内建表：Open-Meteo 的 geocoding 接口只认拉丁/拼音，
 * 实测 `name=泉州` 返回空、`name=Quanzhou` 才有结果 —— 中文站不能把用户当会拼拼音的人。
 */
async function resolveCity(raw: string): Promise<any | null> {
  const name = raw.trim();
  const short = name.replace(/[市县区旗镇乡]$/, "");
  const exact = CITIES.find((c) => c.n === name) ||
    CITIES.find((c) => c.n === short) ||
    CITIES.find((c) => short.length >= 2 && c.n.startsWith(short));
  if (exact) {
    return { name: exact.n, latitude: exact.lat, longitude: exact.lng, admin1: exact.p, adcode: exact.a, country: "中国" };
  }
  try {
    const g = await fetchJson(
      `https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(name)}&count=5&language=zh&format=json`
    );
    const rows = g?.results || [];
    return rows.find((r: any) => r.country_code === "CN") || rows[0] || null;
  } catch {
    return null;
  }
}

async function weatherAction(params: URLSearchParams): Promise<Response> {
  const city = (params.get("city") || "").trim().slice(0, 40);
  if (!city) return json({ ok: false, error: "缺少 city 参数" }, 400);
  const days = Math.max(1, Math.min(16, Number(params.get("days") || 7) || 7));

  const geo = await resolveCity(city);
  if (!geo) {
    return json({
      ok: false,
      error: `没找到「${city}」。本页内置全国地级市与县城坐标；乡镇级地名请用完整应用（它走高德地理编码）。`,
    }, 404);
  }

  const dailyFields = [
    "weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
    "precipitation_probability_max", "wind_speed_10m_max", "relative_humidity_2m_max",
    "uv_index_max", "sunset",
  ].join(",");
  const url = "https://api.open-meteo.com/v1/forecast?latitude=" + geo.latitude +
    "&longitude=" + geo.longitude + "&daily=" + dailyFields +
    "&timezone=Asia/Shanghai&forecast_days=" + days;
  let d: any = null;
  try {
    d = await fetchJson(url);
  } catch (err) {
    return json({ ok: false, error: `预报接口暂时不可用：${(err as Error).message}` }, 502);
  }

  const daily = d?.daily || {};
  const times: string[] = daily.time || [];
  const at = (key: string, i: number) => num((daily[key] || [])[i]);
  const list: Day[] = times.map((iso, i) => {
    const code = at("weather_code", i);
    const info = WMO[code ?? -1] || ["未知", ""];
    return {
      date: iso,
      weekday: weekday(iso),
      text: info[0],
      tmin: at("temperature_2m_min", i),
      tmax: at("temperature_2m_max", i),
      precip: at("precipitation_sum", i) ?? 0,
      prob: at("precipitation_probability_max", i),
      wind: at("wind_speed_10m_max", i),
      humidity: at("relative_humidity_2m_max", i),
      uv: at("uv_index_max", i),
      code,
      sunset: String((daily.sunset || [])[i] || "").slice(11, 16) || undefined,
      comfort: 0,
      verdict: "outdoor",
      advice: "",
      tips: [],
    } as Day;
  });
  for (const f of list) f.comfort = comfortIndex(f);
  // 行程内的相对排名：五天都阴天时，这是唯一真的每天不一样的信息
  const ranked = [...list].sort((a, b) => b.comfort - a.comfort);
  const rankOf = new Map<string, number>();
  ranked.forEach((f, i) => rankOf.set(f.date, i + 1));
  // 极差 <6 时不排名：五天都是 100 分却写"这天体感最弱"是撒谎
  const spread = list.length ? Math.max(...list.map((f) => f.comfort)) - Math.min(...list.map((f) => f.comfort)) : 0;
  for (const f of list) {
    const wet = precipLevel(f.precip, f.prob, f.text);
    const flags = WMO[f.code ?? -1]?.[1] ?? "";
    const blocking = f.precip >= 50 || flags.includes("t") && (f.wind ?? 0) >= 60 || (f.tmax ?? -99) >= 40;
    const risky = f.precip >= 25 || (f.wind ?? 0) >= 55 || (f.tmax ?? -99) >= 37 ||
      f.precip >= 10 || (f.prob ?? 0) >= 70 || flags.includes("f");
    f.verdict = blocking ? "avoid" : risky ? "indoor" : "outdoor";
    f.advice = f.verdict === "avoid"
      ? "该日天气已达停出行级别，建议改期或改成室内半日"
      : f.verdict === "indoor" ? indoorAdvice(f, wet) : outdoorAdvice(f, wet, rankOf.get(f.date) || 0, list.length, spread);
    f.tips = safetyTips(f);
    f.signals = [
      wet !== "无雨" ? wet : "",
      f.wind ? `阵风 ${Math.round(f.wind)}km/h` : "",
      f.humidity !== null ? `湿度 ${Math.round(f.humidity)}%` : "",
      f.uv ? `紫外线 ${f.uv}` : "",
      `舒适指数 ${f.comfort}`,
    ].filter(Boolean);
  }

  const alarms = await cmaAlarms(String(geo.name || city), String(geo.admin1 || ""), String(geo.adcode || ""));
  // 高等级预警直接覆盖结论：宁可保守，也不要漏掉台风天
  for (const a of alarms) {
    const block = (BLOCKING[a.type] || []).includes(a.level) || a.level === "红";
    if (!block) continue;
    const target = list[0];
    if (target) {
      target.verdict = "avoid";
      target.advice = `${a.type}${a.level}色预警已达停出行级别，这天空出来，整体后移或改成室内`;
    }
    break;
  }

  return json({
    ok: true,
    city: geo.name,
    province: geo.admin1 || "",
    country: geo.country || "中国",
    requested: city,
    days_requested: days,
    source: "Open-Meteo 数值预报 + 中国气象局公开预警",
    alarms,
    list,
  });
}

function json(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

/** 完整 Python 应用（魔搭创空间等）转发：没配 Secret 就明确说没配 */
async function forwardPython(action: string, request: Request): Promise<Response> {
  const base = (Deno.env.get("TRIPAGENT_API_BASE") || "").replace(/\/+$/, "");
  const token = Deno.env.get("TRIPAGENT_API_TOKEN") || "";
  if (!base) {
    return json({
      ok: false,
      error: "完整规划引擎尚未接入：本站点已上线天气速查，规划引擎需要一个常驻 Python 服务",
    }, 501);
  }
  const url = `${base}/api/${action}`;
  const headers = new Headers();
  if (token) headers.set("X-API-TOKEN", token);
  const init: RequestInit = {
    method: request.method,
    headers,
    signal: AbortSignal.timeout(120000),
  };
  if (request.method !== "GET") init.body = await request.text();
  try {
    const up = await fetch(url, init);
    return new Response(await up.text(), {
      status: up.status,
      headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
    });
  } catch (err) {
    return json({ ok: false, error: `规划引擎不可达：${(err as Error).message}` }, 502);
  }
}

async function handler(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const action = url.searchParams.get("action") || url.pathname.split("/").filter(Boolean).pop() || "";

  if (request.method !== "GET" && action !== "plan" && action !== "ask") {
    return json({ ok: false, error: "只开放 GET 与 plan/ask 两个 POST 动作" }, 405);
  }
  switch (action) {
    case "weather":
      return await weatherAction(url.searchParams);
    case "plan":
    case "ask":
      return await forwardPython(action, request);
    case "health":
      return json({
        ok: true,
        engine: "tripagent-edge-weather",
        python_configured: Boolean(Deno.env.get("TRIPAGENT_API_BASE")),
        time: new Date().toISOString(),
      });
    default:
      return json({ ok: false, error: `未知动作：${action || "(空)"}` }, 404);
  }
}

Deno.serve(handler);
