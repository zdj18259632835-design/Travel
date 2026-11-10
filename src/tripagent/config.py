"""全局配置：默认值 < .env < 环境变量。

所有"用哪些数据源、按什么顺序降级"的决定都集中在这里，
二次开发新增一个数据源时，只需注册 provider 并把它加进对应 chain，无需改动引擎。
"""

from __future__ import annotations

import functools
import tempfile
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent


def _find_project_root() -> Path:
    for candidate in [PACKAGE_ROOT, *PACKAGE_ROOT.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    return Path.cwd()


PROJECT_ROOT = _find_project_root()

# TRIPAGENT_OFFLINE=1 时只保留这些不联网的 provider（现场断网演示的保险丝）
LOCAL_PROVIDERS = {"fixture", "estimated", "offline", "static", "none"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # 优先级：代码默认值 < 根目录 .env < api_keys/tripagent.env < 系统环境变量
        # 密钥集中在 api_keys/ 目录（已被两层 .gitignore 排除，不会进 GitHub）
        env_file=(PROJECT_ROOT / ".env", PROJECT_ROOT / "api_keys" / "tripagent.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ---- 密钥（留空即视为该 provider 不可用，自动降级）----
    dashscope_api_key: str = Field(default="", alias="DASHSCOPE_API_KEY")
    amap_key: str = Field(default="", alias="AMAP_KEY")
    amap_secret: str = Field(default="", alias="AMAP_SECRET")
    # 腾讯位置服务 Web 服务 Key：只用于与高德交叉校验评分/营业时间/人均，不做主力数据源
    tencent_map_key: str = Field(default="", alias="TENCENT_MAP_KEY")
    qweather_key: str = Field(default="", alias="QWEATHER_KEY")
    qweather_host: str = Field(default="", alias="QWEATHER_HOST")
    # 公网部署时的接口鉴权串；空 = 不鉴权（只允许本机调试）
    api_token: str = Field(default="", alias="TRIPAGENT_API_TOKEN")

    llm_model: str = Field(default="qwen3.6-flash", alias="TRIPAGENT_LLM_MODEL")
    # qwen3 默认开思考模式，实测会把一次抽取型调用从 4 秒拖到 37 秒。
    # 本项目默认关；要恢复推理深度设 TRIPAGENT_LLM_THINKING=1。
    llm_thinking: bool = Field(default=False, alias="TRIPAGENT_LLM_THINKING")

    # ---- 行为开关 ----
    offline: bool = Field(default=False, alias="TRIPAGENT_OFFLINE")
    cache_ttl_hours: float = Field(default=6.0, alias="TRIPAGENT_CACHE_TTL_HOURS")
    http_timeout: float = Field(default=12.0, alias="TRIPAGENT_HTTP_TIMEOUT")
    # 预警接口偶发很慢（实测出现过 48 秒），而它在并发取数那段是"最长的一条路"，
    # 会直接决定整段耗时。给一个更短的专用超时，让它尽快让位给下一个预警源。
    alarm_timeout: float = Field(default=8.0, alias="TRIPAGENT_ALARM_TIMEOUT")
    # 开启 enable_search 的联网归纳实测要 30-120 秒，绝不能复用普通接口的 12 秒超时，
    # 否则表现为"模型无结果"而不是"我等太短了"，非常难查。
    llm_timeout: float = Field(default=150.0, alias="TRIPAGENT_LLM_TIMEOUT")
    # 公开部署的止损闸：每天最多烧多少 token，0 表示不设限。
    # 默认 200 万 ≈ 两百次完整规划，够演示，但刷不掉你的账号。
    llm_daily_token_budget: int = Field(default=2_000_000, alias="TRIPAGENT_LLM_DAILY_TOKEN_BUDGET")
    # 体验口令（逗号分隔）：公开部署时只让主办方进入。留空 = 不启用闸门，本地开发不受影响。
    access_codes: str = Field(default="", alias="TRIPAGENT_ACCESS_CODES")
    # 站长口令：无视日额度上限，到量后仍可继续演示。绝不写进仓库，只放密文变量。
    owner_code: str = Field(default="", alias="TRIPAGENT_OWNER_CODE")

    # ---- 实拍图质量闸门（糊图比没图更伤观感）----
    # 短边像素下限：低于这个数，铺满卡片宽度必然发糊
    image_min_edge: int = Field(default=420, alias="TRIPAGENT_IMAGE_MIN_EDGE")
    # 拉普拉斯方差下限：清晰度指标，越小越糊（失焦/重压缩会掉到个位数）
    image_min_lapvar: float = Field(default=11.0, alias="TRIPAGENT_IMAGE_MIN_LAPVAR")
    http_user_agent: str = "Mozilla/5.0 (compatible; TripAgent/0.1; +https://local.demo)"

    # ---- 数据源降级链（越靠前优先级越高，失败/为空则落到下一个）----
    forecast_chain: str = "qweather,open_meteo,fixture"
    # 空气质量：免密钥、与预报同域名，用来补"要不要戴口罩/有氧放什么时候"
    # 字段名必须等于 "<capability>_chain"，否则 Chain 找不到这条降级链（air_chain 就是踩过的坑）
    air_quality_chain: str = "open_meteo,fixture"
    alarm_chain: str = "qweather,cma_alarm,nmc_alarm,fixture"
    poi_chain: str = "amap,fixture"
    # 双源校验：没配腾讯 Key 时走 none，上层会明确知道"这次没校验"，而不是假装校验过
    poi_crosscheck_chain: str = "tencent,none"
    transit_chain: str = "amap,estimated"
    # 城市解析：先查内建表（免费、离线可用），查不到再用高德地理编码扩展到全国任意县市
    city_chain: str = "static,amap"
    # 住宿口碑特征：有 key 时联网归纳六维，否则退回中性值只按结构化字段排序
    lodging_chain: str = "qwen_search,none"
    culture_chain: str = "qwen_search,fixture"
    # 口碑默认先取本地精选库（毫秒级、可复现、省 token）；
    # 只有本地没有该城市时才会走到联网归纳（单次约 1-2 分钟）。
    # 想要"永远最新"就把顺序换成 "qwen_search,fixture"，代价是每次规划多等几分钟。
    evidence_chain: str = "fixture,qwen_search"
    guide_chain: str = "fixture,qwen_search"
    # 实拍图：高德 POI 图优先（合法、同地点），本地库存过就用，都没有就不显示
    images_chain: str = "amap_search,fixture"
    llm_chain: str = "dashscope"

    # ---- 行程引擎默认偏好 ----
    day_start_minute: int = 510      # 08:30
    day_end_minute: int = 1290       # 21:30
    night_end_minute: int = 1440     # 24:00（酒吧/夜场段）
    max_sights_per_day: int = 3
    max_meals_per_day: int = 3
    max_walk_minutes_per_leg: int = 35

    # ---- 目录 ----
    data_dir: Path = PROJECT_ROOT / "data"
    cache_dir: Path = PROJECT_ROOT / ".cache"
    fixture_dir: Path = PROJECT_ROOT / "data" / "fixtures"
    output_dir: Path = PROJECT_ROOT / "output"

    @property
    def online(self) -> bool:
        return not self.offline

    @property
    def usage_path(self) -> Path:
        """模型用量台账（按天累计 token）。公开部署时的止损依据。"""
        return self.data_dir / "llm_usage.json"

    def chain(self, kind: str) -> list[str]:
        raw = getattr(self, f"{kind}_chain", "") or ""
        names = [n.strip() for n in raw.split(",") if n.strip()]
        if self.offline:
            names = [n for n in names if n in LOCAL_PROVIDERS]
        return names

    def ensure_dirs(self) -> None:
        """可写目录准备。创空间/容器常把代码目录挂成只读，这时把可写目录整体挪到
        临时目录，而不是让第一次写缓存时抛出一个看不懂的 PermissionError。
        """
        probe = self.data_dir / ".write_probe"
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            probe.write_text("1", encoding="utf-8")
            probe.unlink()
        except OSError:
            base = Path(tempfile.gettempdir()) / "tripagent"
            self.data_dir = base / "data"
            self.cache_dir = base / "cache"
            self.output_dir = base / "output"
        for d in (self.data_dir, self.cache_dir, self.fixture_dir, self.output_dir):
            d.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


def reload_settings() -> Settings:
    """Streamlit 里改完 .env 后热重载用。"""
    get_settings.cache_clear()
    return get_settings()
