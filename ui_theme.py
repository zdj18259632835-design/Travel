"""Streamlit 外观层：山海行旅（海青蓝绿 + 落日橙金）。

参考高流量旅行网站（Airbnb 的胶囊搜索卡与照片驱动卡片、Booking/Trip.com 的
蓝色信任感与渐变、去哪儿/飞猪的年轻圆角）再做国风创新：
雪岭头图 + 漂移云 + 汉字徽章时间轴 + 朱砂橙金主按钮。

实现约束：
- 全部内联 CSS，不引外部字体/CDN（内网与断网可演示）；
- 头图走 server.enableStaticServing 的 app/static/hero.jpg，避免每次重跑
  都把 400KB 图片塞进 base64；图片加载失败时底层渐变兜底，页面不塌；
- 控件样式只瞄官方 .st-key-* 类与 data-testid，不碰私有类名；
- 改配色只需要动 :root 里的变量。
"""

from __future__ import annotations

from html import escape as _e

import streamlit as st

CSS = """
<style>
/* ---------- 设计令牌：海青 + 落日橙金 ---------- */
:root {
    --navy:#0F2E3D; --ink:#132A35; --sub:#51666F;
    --teal:#0B7285; --azure:#1098AD; --sky:#339AF0;
    --coral:#FF6B35; --amber:#FFB020; --gold:#E8B54A;
    --sand:#F7F3EC; --mist:#F2F7F9;
    --ok:#2F9E6E; --warn:#E8A013; --bad:#E03131; --night:#7048E8;
    --serif:"STZhongsong","SimSun","Songti SC",serif;
    --sans:"PingFang SC","Microsoft YaHei","Source Han Sans SC",sans-serif;
    --shadow:0 10px 30px rgba(15,46,61,.10);
    --lift:0 18px 44px rgba(15,46,61,.16);
}
html, body, [class*="css"] { font-family: var(--sans); }
.stApp {
    background:
        radial-gradient(1100px 480px at 8% -6%, rgba(16,152,173,.14), transparent 62%),
        radial-gradient(900px 420px at 96% -2%, rgba(255,107,53,.10), transparent 58%),
        radial-gradient(760px 500px at 50% 108%, rgba(232,181,74,.10), transparent 60%),
        var(--mist);
}
header[data-testid="stHeader"] { background: transparent; }

/* ---------- 品牌条 ---------- */
.tx-brand { display:flex; align-items:center; gap:12px; padding:8px 4px 14px; }
.tx-brand .seal {
    width:42px; height:42px; border-radius:13px; flex:none;
    background:linear-gradient(135deg,var(--coral),var(--amber));
    color:#fff; font-family:var(--serif); font-size:23px;
    display:flex; align-items:center; justify-content:center;
    box-shadow:0 6px 16px rgba(255,107,53,.35);
}
.tx-brand .word { font-family:var(--serif); font-size:22px; color:var(--navy); letter-spacing:.04em; line-height:1.15; }
.tx-brand .word small { display:block; font-size:11px; color:var(--sub); letter-spacing:.34em; font-family:var(--sans); margin-top:2px; }
.tx-brand .chip {
    margin-left:auto; font-size:12px; color:var(--teal);
    background:rgba(16,152,173,.10); border:1px solid rgba(16,152,173,.25);
    padding:5px 13px; border-radius:999px; white-space:nowrap;
}

/* ---------- 主导航：悬浮胶囊页签（滚动时吸顶） ----------
   1.66 起页签换成 react-aria（div[role=tablist] + div[data-testid=stTab]），
   旧版仍是 baseweb；两套选择器都写上，requirements 允许 >=1.40。 */
div[role="tablist"], div[data-baseweb="tab-list"] {
    position:sticky; top:6px; z-index:50;
    background:rgba(247,243,236,.88); backdrop-filter:blur(12px);
    border-radius:999px; padding:6px; gap:4px;
    border:1px solid rgba(15,46,61,.08); box-shadow:var(--shadow);
}
div[data-testid="stTab"], button[data-baseweb="tab"] {
    border-radius:999px !important; color:var(--sub); background:transparent;
    border:none; padding:8px 18px; font-weight:600; letter-spacing:.02em;
    transition:color .18s, background .18s;
}
div[data-testid="stTab"] p, button[data-baseweb="tab"] p { color:inherit; margin:0; }
div[data-testid="stTab"]:hover, button[data-baseweb="tab"]:hover {
    color:var(--teal); background:rgba(16,152,173,.08);
}
div[data-testid="stTab"][aria-selected="true"], button[data-baseweb="tab"][aria-selected="true"] {
    background:linear-gradient(135deg,var(--teal),var(--azure)) !important; color:#fff !important;
    box-shadow:0 6px 16px rgba(11,114,133,.30);
}
div[data-testid="stTab"][aria-selected="true"] p { color:#fff; }
.react-aria-SelectionIndicator, div[data-baseweb="tab-highlight"], div[data-baseweb="tab-border"] { display:none !important; }
/* 行程内的「按天页签」不吸顶，做成书签 */
.st-key-daytabs div[role="tablist"], .st-key-daytabs div[data-baseweb="tab-list"] {
    position:static; background:transparent; backdrop-filter:none;
    border:none; box-shadow:none; border-radius:0; padding:0; gap:6px;
    border-bottom:2px solid rgba(15,46,61,.08);
}
.st-key-daytabs div[data-testid="stTab"], .st-key-daytabs button[data-baseweb="tab"] {
    border-radius:12px 12px 0 0 !important; padding:8px 16px;
    border:1px solid transparent; border-bottom:none; box-shadow:none;
}
.st-key-daytabs div[data-testid="stTab"][aria-selected="true"],
.st-key-daytabs button[data-baseweb="tab"][aria-selected="true"] {
    background:#fff !important; color:var(--teal) !important; box-shadow:none;
    border-color:rgba(15,46,61,.10); border-bottom:3px solid var(--coral);
}
.st-key-daytabs div[data-testid="stTab"][aria-selected="true"] p { color:var(--teal); }

/* ---------- 头图：雪岭冬阳 + 漂移云 ---------- */
.tx-hero {
    position:relative; height:400px; border-radius:22px; overflow:hidden;
    margin:6px 0 0; box-shadow:var(--lift);
    background:linear-gradient(135deg,#0F2E3D,#0B7285 55%,#1098AD);
}
.tx-hero img.bg { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }
.tx-hero .scrim {
    position:absolute; inset:0;
    background:linear-gradient(100deg, rgba(10,32,43,.80) 0%, rgba(10,32,43,.46) 42%, rgba(10,32,43,.04) 78%);
}
.tx-hero .clouds { position:absolute; inset:0; pointer-events:none; overflow:hidden; }
.tx-hero .clouds svg { position:absolute; width:520px; opacity:.42; }
.tx-hero .c1 { top:11%; left:-560px; animation:txdrift 46s linear infinite; }
.tx-hero .c2 { top:36%; left:-560px; width:380px; opacity:.28; animation:txdrift 64s linear infinite; animation-delay:-24s; }
@keyframes txdrift { from { transform:translateX(0); } to { transform:translateX(2100px); } }
.tx-hero .copy {
    position:absolute; left:40px; bottom:104px; max-width:660px; color:#fff;
    animation:txrise .9s cubic-bezier(.2,.7,.2,1) both;
}
.tx-hero .kicker { font-size:12px; letter-spacing:.30em; color:var(--amber); font-weight:700; margin-bottom:10px; }
.tx-hero h1 {
    font-family:var(--serif); font-size:clamp(28px,3.6vw,46px); line-height:1.22;
    margin:0 0 10px; letter-spacing:.05em; text-shadow:0 3px 18px rgba(6,24,33,.45);
}
.tx-hero h1 em { font-style:normal; color:var(--amber); }
.tx-hero p { margin:0 0 14px; font-size:15px; line-height:1.7; color:rgba(255,255,255,.88); max-width:560px; }
.tx-hero .trust { display:flex; flex-wrap:wrap; gap:8px; }
.tx-hero .trust span {
    font-size:12px; padding:4px 12px; border-radius:999px;
    background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.30);
    backdrop-filter:blur(4px); color:#fff;
}
@keyframes txrise { from { opacity:0; transform:translateY(22px); } to { opacity:1; transform:none; } }

/* ---------- 搜索卡：压住头图下缘（Airbnb 式胶囊） ---------- */
.st-key-searchcard {
    margin-top:-70px; position:relative; z-index:5;
    background:#fff !important; border-radius:20px !important;
    border:1px solid rgba(15,46,61,.06) !important;
    box-shadow:var(--lift) !important; padding:8px 12px;
    animation:txrise .9s .12s cubic-bezier(.2,.7,.2,1) both;
}
.tx-lab { font-size:12px; font-weight:700; letter-spacing:.14em; color:var(--sub); margin:2px 0 6px; }
.st-key-searchcard .stTextInput input {
    height:50px; border-radius:999px !important; padding:0 20px; font-size:16px;
}
.st-key-searchcard .stButton > button[kind="primary"] { border-radius:999px; height:50px; font-size:16px; }
.tx-note { font-size:12.5px; color:var(--sub); margin-top:8px; line-height:1.6; }

/* ---------- 快捷灵感 chips ---------- */
.st-key-quickchips .stButton > button {
    border-radius:999px; border:1px solid rgba(16,152,173,.35);
    color:var(--teal); background:rgba(16,152,173,.06);
    padding:6px 14px; font-size:13px; font-weight:600;
}
.st-key-quickchips .stButton > button:hover {
    background:rgba(16,152,173,.14); border-color:var(--azure); color:var(--teal);
}

/* ---------- 章节头：壹贰叁肆 ---------- */
.tx-sec { display:flex; align-items:baseline; gap:12px; margin:28px 0 14px; }
.tx-sec .no { font-family:var(--serif); color:var(--gold); font-size:20px; flex:none; }
.tx-sec h2 { font-family:var(--serif); font-size:24px; color:var(--navy); margin:0; letter-spacing:.06em; flex:none; }
.tx-sec .rule { flex:1; height:2px; border-radius:2px; background:linear-gradient(90deg, rgba(232,181,74,.7), rgba(232,181,74,0)); }
.tx-sec .hint { font-size:12.5px; color:var(--sub); flex:none; }

/* ---------- 按钮：朱砂橙金主按钮 + 青绿描边次按钮 ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
    border-radius:12px; border:1px solid rgba(11,114,133,.35);
    color:var(--teal); background:#fff; font-weight:600;
    transition:transform .18s, box-shadow .18s, background .18s, border-color .18s;
}
.stButton > button:hover, .stDownloadButton > button:hover {
    background:rgba(16,152,173,.07); border-color:var(--azure); color:var(--teal);
    transform:translateY(-1px);
}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
    background:linear-gradient(135deg,var(--coral),#FF9F1C); color:#fff; border:none;
    font-weight:700; letter-spacing:.08em;
    box-shadow:0 8px 20px rgba(255,107,53,.30);
}
.stButton > button[kind="primary"]:hover {
    background:linear-gradient(135deg,#FF7C4D,#FFAD33); color:#fff;
    transform:translateY(-2px); box-shadow:0 12px 26px rgba(255,107,53,.38);
}
.stButton > button[kind="primary"]:disabled,
.stButton > button[kind="primary"][disabled] {
    background:#D7DEE2; color:#8A99A1; box-shadow:none; transform:none;
}

/* ---------- 输入控件 ---------- */
.stTextInput input, .stTextArea textarea, .stNumberInput input, .stDateInput input {
    background:#fff !important; border:1.5px solid rgba(15,46,61,.14) !important;
    border-radius:12px !important; color:var(--ink);
}
.stTextInput input:focus, .stTextArea textarea:focus, .stNumberInput input:focus, .stDateInput input:focus {
    border-color:var(--azure) !important; box-shadow:0 0 0 3px rgba(51,154,240,.18) !important;
}
.stSelectbox div[data-baseweb="select"] > div, .stMultiSelect div[data-baseweb="select"] > div {
    background:#fff !important; border:1.5px solid rgba(15,46,61,.14) !important; border-radius:12px !important;
}
label p, .stTextInput label, .stSelectbox label, .stSlider label, .stNumberInput label,
.stDateInput label, .stMultiSelect label, .stCheckbox label, .stToggle label {
    color:var(--ink); font-weight:600; font-size:13.5px;
}
[data-baseweb="slider"] > div > div > div { background:var(--azure) !important; }
[data-baseweb="slider"] div[role="slider"] { background:var(--coral) !important; border-color:var(--coral) !important; }
small, .stCaption, [data-testid="stCaptionContainer"] { color:var(--sub); }

/* ---------- 卡片 ---------- */
/* 旧 DOM（≤1.5x）：border=True 的白卡是 stVerticalBlockBorderWrapper */
div[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius:16px; background:#fff;
    border:1px solid rgba(15,46,61,.07); box-shadow:var(--shadow);
    transition:transform .2s, box-shadow .2s;
}
div[data-testid="stVerticalBlockBorderWrapper"]:hover { transform:translateY(-3px); box-shadow:var(--lift); }
/* 新 DOM（1.66+）：BorderWrapper 已移除，边框直接落在带 key 的 stVerticalBlock 上，
   父级是 stLayoutWrapper。用直接子选择器精确命中，避免在旧 DOM 上叠成双层卡。 */
div[data-testid="stLayoutWrapper"] > div[class*="st-key-slot-"],
div[data-testid="stLayoutWrapper"] > .st-key-askbox,
div[data-testid="stLayoutWrapper"] > div[class*="st-key-askitem-"],
div[data-testid="stLayoutWrapper"] > div[class*="st-key-histcard-"] {
    background:#fff; border-radius:16px;
    border:1px solid rgba(15,46,61,.07); box-shadow:var(--shadow);
    transition:transform .2s, box-shadow .2s;
}
div[data-testid="stLayoutWrapper"] > div[class*="st-key-slot-"]:hover,
div[data-testid="stLayoutWrapper"] > div[class*="st-key-askitem-"]:hover,
div[data-testid="stLayoutWrapper"] > div[class*="st-key-histcard-"]:hover {
    transform:translateY(-3px); box-shadow:var(--lift);
}
/* 行程条目按活动类型着色左侧脊线：day=海青 meal=落日 night=夜紫 */
div[class*="st-key-slot-day"] { border-left:4px solid var(--azure) !important; }
div[class*="st-key-slot-meal"] { border-left:4px solid var(--coral) !important; }
div[class*="st-key-slot-night"] { border-left:4px solid var(--night) !important; }

div[data-testid="stExpander"] details {
    background:#fff; border:1px solid rgba(15,46,61,.08); border-radius:14px; box-shadow:var(--shadow);
}
div[data-testid="stExpander"] summary { font-weight:600; color:var(--navy); }
[data-testid="stMetric"] {
    background:#fff; border:1px solid rgba(15,46,61,.07); border-radius:14px;
    padding:12px 14px; box-shadow:var(--shadow);
}
[data-testid="stMetricValue"] { color:var(--teal); font-weight:800; }
[data-testid="stMetricLabel"] { color:var(--sub); }
div[data-testid="stAlert"] { border-radius:12px; border-width:0 0 0 4px; }
[data-testid="stDataFrame"] { border-radius:14px; overflow:hidden; border:1px solid rgba(15,46,61,.08); }
hr { border-color:rgba(15,46,61,.10); }

/* ---------- 天气结论：整行宽卡 + 每天一列（宽屏铺满，不空右边） ---------- */
.tx-wgrid { display:flex; flex-direction:column; gap:14px; margin:10px 0 4px; }
.tx-wcard {
    position:relative; overflow:hidden; background:#fff; border-radius:16px;
    border:1px solid rgba(15,46,61,.07); border-top:4px solid var(--ok);
    box-shadow:var(--shadow); padding:18px 20px 16px;
}
.tx-wcard.warn { border-top-color:var(--warn); }
.tx-wcard.bad { border-top-color:var(--bad); }
.tx-wcard .glow {
    position:absolute; right:-30px; top:-30px; width:130px; height:130px;
    border-radius:50%; filter:blur(34px); opacity:.28; background:var(--ok); pointer-events:none;
}
.tx-wcard.warn .glow { background:var(--warn); }
.tx-wcard.bad .glow { background:var(--bad); }
.tx-wcard .whead { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-bottom:14px; }
.tx-wcard .t { font-size:15px; font-weight:700; letter-spacing:.06em; color:var(--sub); }
.tx-wcard .n { font-family:var(--serif); font-size:30px; color:var(--navy); line-height:1; }
.tx-wcard .n small { font-size:14px; color:var(--sub); font-family:var(--sans); margin-left:6px; }
/* flex 而不是 grid：auto-fit 在"这组只有 1 天"时会留下 0px 幽灵轨道，
   它的 gap 仍占 18px，右缘白白缩进一条；flex 伸缩没有这个问题 */
.tx-wcard .wcols { display:flex; flex-wrap:wrap; gap:18px; }
.tx-wcard .wcol { flex:1 1 260px; min-width:0; border-left:3px solid rgba(15,46,61,.08); padding-left:16px; }
.tx-wcard .wcol:first-child { border-left:none; padding-left:0; }
.tx-wcard .wd { font-size:14px; font-weight:700; color:var(--navy); letter-spacing:.04em; margin-bottom:6px; }
.tx-wcard .row { font-size:15px; color:var(--ink); line-height:1.6; }
.tx-wcard .row b { color:var(--navy); font-size:16px; }
.tx-wcard .row .why { display:block; color:var(--sub); font-size:13.5px; line-height:1.55; margin-top:3px; }
.tx-wcard .adv { color:var(--teal); font-size:15px; line-height:1.6; margin-top:8px; }
.tx-wcard .tip { color:var(--sub); font-size:13.5px; line-height:1.55; margin-top:8px; }
.tx-alarms { margin-top:10px; }
@media (max-width:860px) {
    .tx-wcard .wcols { flex-direction:column; gap:12px; }
    .tx-wcard .wcol { border-left:none; padding-left:0; border-top:1px dashed rgba(15,46,61,.10); padding-top:12px; }
    .tx-wcard .wcol:first-child { border-top:none; padding-top:0; }
}

/* ---------- 一天的时间轴 ---------- */
div[class*="st-key-dayline"] { position:relative; padding-left:34px; }
div[class*="st-key-dayline"]::before {
    content:""; position:absolute; left:11px; top:8px; bottom:8px; width:2px; border-radius:2px;
    background:linear-gradient(180deg,var(--azure),var(--gold) 55%,var(--night)); opacity:.55;
}
.tx-band {
    position:relative; font-family:var(--serif); font-size:17px; letter-spacing:.2em;
    color:var(--navy); margin:18px 0 6px; font-weight:700;
}
.tx-band .dot {
    position:absolute; left:-29px; top:7px; width:12px; height:12px; border-radius:50%;
    background:var(--azure); border:3px solid #fff; box-shadow:0 0 0 2px rgba(16,152,173,.4);
}
.tx-band.meal .dot { background:var(--coral); box-shadow:0 0 0 2px rgba(255,107,53,.4); }
.tx-band.night .dot { background:var(--night); box-shadow:0 0 0 2px rgba(112,72,232,.4); }

/* ---------- 行程条目头部：汉字徽章 + 标题 + 徽标 ---------- */
.tx-head { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:2px 0 6px; }
.tx-med {
    width:38px; height:38px; border-radius:12px; flex:none;
    display:flex; align-items:center; justify-content:center;
    font-family:var(--serif); font-size:19px; color:#fff;
    background:linear-gradient(135deg,var(--azure),var(--sky));
    box-shadow:0 5px 12px rgba(16,152,173,.30);
}
.tx-med.meal { background:linear-gradient(135deg,var(--coral),var(--amber)); box-shadow:0 5px 12px rgba(255,107,53,.30); }
.tx-med.night { background:linear-gradient(135deg,var(--night),#9775FA); box-shadow:0 5px 12px rgba(112,72,232,.30); }
.tx-title { font-size:16.5px; font-weight:800; color:var(--navy); }
.tx-badge {
    font-size:12px; padding:3px 10px; border-radius:999px;
    background:rgba(16,152,173,.10); color:var(--teal); border:1px solid rgba(16,152,173,.22);
}
.tx-badge.free { background:rgba(47,158,110,.12); color:#22795A; border-color:rgba(47,158,110,.30); }
.tx-badge.price { background:rgba(255,176,32,.14); color:#9A6B00; border-color:rgba(255,176,32,.40); }
.tx-return {
    margin-top:8px; font-size:12.5px; color:#5F3DC4; line-height:1.55;
    background:rgba(112,72,232,.08); border:1px dashed rgba(112,72,232,.35);
    border-radius:10px; padding:6px 10px;
}

/* ---------- 正文行：字号与颜色沿用用户两轮反馈后的定稿 ---------- */
.slotline { color:#12201A; font-size:15.5px; line-height:1.62; margin:2px 0 0; }
.slotline b { color:#12201A; }
.bandnote { color:#12201A; font-size:15px; line-height:1.6; margin:0 0 4px; }
.pitline { color:#9A4526; font-size:14px; line-height:1.55; margin:6px 0 0; }
.metaline { color:#45584F; font-size:14px; line-height:1.55; margin:4px 0 0; }

/* ---------- 诗词彩蛋：金笺 ---------- */
.poem {
    margin:8px 0 0; padding:8px 12px; border-radius:10px;
    background:linear-gradient(135deg, rgba(232,181,74,.12), rgba(232,181,74,.04));
    border-left:3px solid var(--gold);
    font-family:var(--serif); color:#7A5B1E; font-size:13.5px;
}
.poem .v { letter-spacing:.04em; }
.poem .m { color:#9A855A; font-size:12px; margin-top:2px; font-family:var(--sans); }

/* ---------- 校验记分卡：环形通过率 + 逐条清单 ---------- */
.tx-scoregrid { display:grid; grid-template-columns:230px 1fr; gap:16px; align-items:start; }
.tx-donutcard {
    background:#fff; border-radius:16px; border:1px solid rgba(15,46,61,.07);
    box-shadow:var(--shadow); padding:18px; text-align:center;
}
.tx-donut {
    width:150px; height:150px; border-radius:50%; margin:6px auto 10px;
    position:relative; display:flex; align-items:center; justify-content:center;
}
.tx-donut::before { content:""; position:absolute; inset:14px; background:#fff; border-radius:50%; }
.tx-donut b { position:relative; font-family:var(--serif); font-size:32px; color:var(--navy); }
.tx-donut b small { font-size:14px; color:var(--sub); }
.tx-donutcard .cap { font-size:12.5px; color:var(--sub); line-height:1.6; }
.tx-checks { display:flex; flex-direction:column; gap:8px; }
.tx-mini { display:flex; gap:8px; margin-bottom:10px; flex-wrap:wrap; }
.tx-mini span {
    font-size:12px; padding:4px 12px; border-radius:999px;
    background:rgba(16,152,173,.08); color:var(--teal); border:1px solid rgba(16,152,173,.20);
}
.tx-checkrow {
    display:flex; gap:10px; align-items:flex-start; background:#fff;
    border:1px solid rgba(15,46,61,.07); border-radius:12px; padding:9px 12px;
    box-shadow:0 2px 8px rgba(15,46,61,.05);
}
.tx-checkrow .ic {
    width:22px; height:22px; border-radius:50%; flex:none; margin-top:1px;
    display:flex; align-items:center; justify-content:center;
    font-size:12px; color:#fff; background:var(--ok);
}
.tx-checkrow.fail .ic { background:var(--bad); }
.tx-checkrow .kind {
    font-size:11px; color:var(--sub); background:rgba(15,46,61,.06);
    padding:2px 8px; border-radius:999px; flex:none; margin-top:2px; white-space:nowrap;
}
.tx-checkrow p { margin:0; font-size:13.5px; color:var(--ink); line-height:1.5; }
.tx-checkrow p b { color:var(--navy); }
.tx-checkrow p .fix { color:#9A6B00; }
@media (max-width:860px) { .tx-scoregrid { grid-template-columns:1fr; } }

/* ---------- 历史行程封面卡 ---------- */
.tx-cover {
    position:relative; height:96px; border-radius:14px; overflow:hidden;
    display:flex; align-items:flex-end; padding:10px 14px; margin-bottom:10px;
}
.tx-cover.c0 { background:linear-gradient(135deg,#0B7285,#339AF0); }
.tx-cover.c1 { background:linear-gradient(135deg,#FF6B35,#FFB020); }
.tx-cover.c2 { background:linear-gradient(135deg,#7048E8,#339AF0); }
.tx-cover::after {
    content:""; position:absolute; inset:0;
    background:radial-gradient(220px 90px at 85% 0%, rgba(255,255,255,.35), transparent 70%);
}
.tx-cover b {
    position:relative; color:#fff; font-family:var(--serif); font-size:19px;
    letter-spacing:.08em; text-shadow:0 2px 8px rgba(0,0,0,.25);
}
.tx-meta { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0; }
.tx-meta i {
    font-style:normal; font-size:12px; padding:3px 10px; border-radius:999px;
    background:rgba(15,46,61,.05); color:var(--sub); border:1px solid rgba(15,46,61,.08);
}

/* ---------- 实拍图：统一画幅上限，绝不裁切 ---------- */
[data-testid="stImage"] img {
    width: auto !important; height: auto !important;
    max-width: 100%; max-height: 46vh;
    margin:0 auto; display:block; border-radius:14px;
}
[data-testid="stImageContainer"] { text-align:center; }
[data-testid="stImageCaption"] { font-size:12.5px; color:var(--sub); }
[data-testid="stDialog"] [data-testid="stImage"] img { max-height:76vh; }

/* ---------- 滚动条也走山海配色 ---------- */
*::-webkit-scrollbar { width:10px; height:10px; }
*::-webkit-scrollbar-track { background:rgba(15,46,61,.05); border-radius:8px; }
*::-webkit-scrollbar-thumb {
    background:linear-gradient(180deg,var(--azure),var(--teal));
    border-radius:8px; border:2px solid var(--mist);
}
*::-webkit-scrollbar-thumb:hover { background:var(--coral); }

/* ---------- 杂项与动效克制 ---------- */
#MainMenu, footer { visibility:hidden; }
[data-testid="stSidebar"] { display:none; }
@media (max-width:860px) {
    .tx-hero { height:320px; }
    .tx-hero .copy { left:20px; right:20px; bottom:92px; }
    .tx-brand .chip { display:none; }
}
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation:none !important; transition:none !important; }
}
</style>
"""

_BRAND = """
<div class="tx-brand">
  <div class="seal">行</div>
  <div class="word">TripAgent<small>山海行旅</small></div>
  <div class="chip">16 天预报 · 18 项约束校验</div>
</div>
"""

_HERO = """
<div class="tx-hero">
  <img class="bg" src="app/static/hero.jpg" alt="">
  <div class="scrim"></div>
  <div class="clouds">
    <svg class="c1" viewBox="0 0 520 120" aria-hidden="true">
      <g fill="#ffffff">
        <ellipse cx="110" cy="78" rx="90" ry="26"/>
        <ellipse cx="200" cy="60" rx="76" ry="32"/>
        <ellipse cx="300" cy="78" rx="96" ry="24"/>
        <ellipse cx="390" cy="66" rx="60" ry="22"/>
      </g>
    </svg>
    <svg class="c2" viewBox="0 0 520 120" aria-hidden="true">
      <g fill="#ffffff">
        <ellipse cx="140" cy="76" rx="100" ry="24"/>
        <ellipse cx="250" cy="58" rx="80" ry="30"/>
        <ellipse cx="360" cy="76" rx="90" ry="22"/>
      </g>
    </svg>
  </div>
  <div class="copy">
    <div class="kicker">天池 2026 · LIFE HACK 生活娱乐</div>
    <h1>说一句话，<em>山海</em>便如约而至</h1>
    <p>会看天气的旅行管家：极端天气主动避让，每条推荐都有依据，每一项安排都逐条通过约束校验。</p>
    <div class="trust"><span>零 Key 离线可跑</span><span>数据源可回溯</span><span>18 项约束记分卡</span></div>
  </div>
</div>
"""


def inject() -> None:
    """注入全局样式。放在 set_page_config 之后、任何业务渲染之前调用一次。"""
    st.markdown(CSS, unsafe_allow_html=True)


def brand() -> None:
    st.markdown(_BRAND, unsafe_allow_html=True)


def hero() -> None:
    st.markdown(_HERO, unsafe_allow_html=True)


def section(no: str, title: str, hint: str = "") -> None:
    """章节头：汉字序号 + 标题 + 金线 + 右侧一句提示。"""
    h = f'<span class="hint">{_e(hint)}</span>' if hint else ""
    st.markdown(
        f'<div class="tx-sec"><span class="no">{_e(no)}</span>'
        f'<h2>{_e(title)}</h2><div class="rule"></div>{h}</div>',
        unsafe_allow_html=True,
    )
