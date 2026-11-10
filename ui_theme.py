"""Streamlit 外观层：青绿山水 + 宣纸质感。

刻意全部用内联 CSS 与 SVG，不引入任何图片文件与外部字体/CDN：
一是评委与用户可能在内网/断网环境打开，二是站点部署时少一个依赖。
改配色只需要动 :root 里的几个变量，不动逻辑代码。
"""

from __future__ import annotations

import streamlit as st

CSS = """
<style>
/* ---------- 底色与排版：宣纸 + 松墨 ---------- */
html, body, [class*="css"] {
    font-family: "PingFang SC", "Microsoft YaHei", "Source Han Sans SC", sans-serif;
}
.stApp {
    background:
        radial-gradient(1200px 420px at 12% -8%, rgba(122,166,148,.20), transparent 62%),
        radial-gradient(900px 380px at 92% -4%, rgba(93,138,166,.16), transparent 60%),
        #f6f4ec;
}
header[data-testid="stHeader"] { background: transparent; }

/* ---------- 顶部山水横幅 ---------- */
.shanshui-hero {
    position: relative; margin: -1.2rem -1.2rem 1.4rem; padding: 26px 26px 8px;
    border-bottom: 1px solid rgba(61,107,90,.16); overflow: hidden;
}
.shanshui-hero h1 {
    font-size: 1.9rem; font-weight: 700; letter-spacing: .06em; margin: 0;
    color: #26332f;
    font-family: "STZhongsong", "SimSun", "Songti SC", serif;
}
.shanshui-hero .seal {
    display: inline-block; margin-left: 10px; padding: 2px 8px; border-radius: 4px;
    background: #b4552f; color: #fbf7ef; font-size: .72rem; letter-spacing: .14em;
    vertical-align: middle; font-family: "STZhongsong", "SimSun", serif;
}
.shanshui-hero .verse {
    margin-top: 6px; color: #5d6f68; font-size: .84rem; letter-spacing: .12em;
    font-family: "STKaiti", "KaiTi", "Kaiti SC", serif;
}
.shanshui-hero svg { display: block; width: 100%; height: 74px; margin-top: 6px; }

/* ---------- 卡片与区块 ---------- */
[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"],
.stMarkdown div[style*="border"] { border-radius: 14px; }
div[data-testid="stExpander"] details {
    background: rgba(255,255,255,.72); border: 1px solid rgba(61,107,90,.16); border-radius: 12px;
}
/* 指标卡：去掉生硬边框，换成宣纸浮起感 */
[data-testid="stMetric"] {
    background: rgba(255,255,255,.66); border: 1px solid rgba(61,107,90,.14);
    border-radius: 12px; padding: 10px 12px;
}
[data-testid="stMetricValue"] { color: #2f5d4e; font-weight: 700; }

/* ---------- 页签：做成书签 ---------- */
button[role="tab"] {
    border-radius: 10px 10px 0 0; padding: 8px 14px; letter-spacing: .04em;
    color: #5d6f68; border: 1px solid rgba(61,107,90,.18); border-bottom: none;
    background: rgba(255,255,255,.45);
}
button[role="tab"][aria-selected="true"] {
    color: #fbf7ef; background: #3d6b5a; border-color: #3d6b5a; font-weight: 600;
}

/* ---------- 按钮：朱砂与青绿 ---------- */
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
    background: linear-gradient(180deg, #47796a, #35604f); color: #fbf7ef;
    border: none; border-radius: 10px; padding: 10px 0; font-size: 1rem; letter-spacing: .1em;
    box-shadow: 0 2px 0 rgba(53,96,79,.35);
}
.stButton > button[kind="primary"]:hover { background: linear-gradient(180deg, #4f8878, #3b6b58); }
.stButton > button { border-radius: 10px; border-color: rgba(61,107,90,.3); color: #2f5d4e; }

/* ---------- 输入控件 ---------- */
.stTextInput input, .stTextArea textarea, .stNumberInput input, .stDateInput input,
.stSelectbox div[data-baseweb="select"] > div, .stMultiSelect div[data-baseweb="select"] > div {
    background: rgba(255,255,255,.78) !important;
    border: 1px solid rgba(61,107,90,.22) !important; border-radius: 10px !important;
}
[data-baseweb="slider"] [data-baseweb="slider"] > div > div { background: #3d6b5a; }
[data-baseweb="slider"] [role="slider"] { background: #b4552f; border-color: #b4552f; }
label, .stMarkdown p { color: #3a4a44; }
small, .stCaption, [data-testid="stCaptionContainer"] { color: #6b7d76; }

/* ---------- 提示条与表格 ---------- */
div[data-testid="stAlert"] { border-radius: 12px; border-width: 0 0 0 4px; }
[data-testid="stDataFrame"] { border-radius: 12px; overflow: hidden; }
hr { border-color: rgba(61,107,90,.18); }

/* ---------- 行程条目 ---------- */
.band { margin: 14px 0 6px; }
.bandname {
    font-family: "STZhongsong", "SimSun", serif; font-size: .95rem; letter-spacing: .18em;
    color: #2f5d4e; padding-left: 10px; border-left: 3px solid #b4552f; margin-bottom: 6px;
}
.slot {
    background: rgba(255,255,255,.6); border: 1px solid rgba(61,107,90,.13);
    border-left: 3px solid #6b9b7c; border-radius: 10px; padding: 9px 12px; margin: 7px 0;
}
.slot.night { border-left-color: #7b5ea7; }
.slot.meal { border-left-color: #b4552f; }
.slot .why { color: #6b7d76; font-size: .82rem; margin-top: 3px; }
.slot .tag {
    display: inline-block; font-size: .72rem; padding: 1px 8px; border-radius: 999px;
    background: rgba(107,155,124,.14); color: #3d6b5a; margin-left: 6px;
}
.slot .tag.n { background: rgba(123,94,167,.14); color: #6b4f96; }
.slot .tag.res { background: rgba(180,85,47,.14); color: #9a4526; }

/* ---------- 侧边栏彻底隐藏（Key 状态与离线开关已移入自检页）---------- */
#MainMenu, footer { visibility: hidden; }
[data-testid="stSidebar"] { display: none; }
</style>
"""

_HERO = """
<div class="shanshui-hero">
  <h1>TripAgent<span class="seal">山水行旅</span></h1>
  <div class="verse">远上寒山、青绿千载 —— 先看天气，再论行程</div>
  <svg viewBox="0 0 1200 74" preserveAspectRatio="none" aria-hidden="true">
    <defs>
      <linearGradient id="far" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#8fb3a6" stop-opacity=".55"/>
        <stop offset="100%" stop-color="#8fb3a6" stop-opacity=".12"/>
      </linearGradient>
      <linearGradient id="near" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#3d6b5a" stop-opacity=".72"/>
        <stop offset="100%" stop-color="#3d6b5a" stop-opacity=".22"/>
      </linearGradient>
    </defs>
    <path fill="url(#far)" d="M0,58 L90,26 L170,46 L260,14 L350,44 L450,20 L540,50 L640,24 L740,52 L840,30 L950,54 L1060,32 L1200,56 L1200,74 L0,74 Z"/>
    <path fill="url(#near)" d="M0,66 L120,40 L230,60 L330,34 L440,62 L560,42 L680,66 L800,46 L920,68 L1040,50 L1200,70 L1200,74 L0,74 Z"/>
    <circle cx="1105" cy="20" r="9" fill="#b4552f" opacity=".5"/>
  </svg>
</div>
"""


def inject() -> None:
    """注入全局样式。放在 set_page_config 之后、任何业务渲染之前调用一次。"""
    st.markdown(CSS, unsafe_allow_html=True)


def hero() -> None:
    st.markdown(_HERO, unsafe_allow_html=True)
