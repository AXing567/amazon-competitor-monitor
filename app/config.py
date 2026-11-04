"""全局配置：从 .env 读取，全项目唯一配置入口。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 服务 ----
    app_host: str = "0.0.0.0"
    app_port: int = 8100
    app_env: str = "dev"
    log_level: str = "INFO"

    # ---- 数据库 ----
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'competitor_monitor.db'}"

    # ---- 卖家精灵转接桥 ----
    sellersprite_bridge_url: str = "http://127.0.0.1:8000"
    sellersprite_timeout: int = 60

    # ---- Amazon SP-API（自家数据；竞品不走这里）----
    # 三项 LWA 凭据齐了才算「已配置」。缺任意一项时 `SpApiAdapter.configured` 为 False，
    # 取数会抛 `SpApiNotConfigured`（带可照做的补法），而不是返回一个空对象假装成功。
    #
    # refresh_token 的拿法：Seller Central → 设置 → 用户权限 → 开发者中心，
    # 建一个应用（勾上库存/订单/定价等需要的角色），审核通过后在应用页「授权」拿 token。
    spapi_refresh_token: str = ""
    spapi_client_id: str = ""
    spapi_client_secret: str = ""
    # na(北美) / eu(欧洲、中东、印度) / fe(日本、澳洲)。与 marketplace 必须匹配，否则请求 403/404。
    spapi_region: str = "na"
    # 默认 ATVPDKIKX0DER = 美国站。站点代码到 ID 的映射在 `adapters/spapi.py`。
    spapi_marketplace_id: str = "ATVPDKIKX0DER"

    # ---- DeepSeek ----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"

    # ---- 飞书 ----
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_default_chat_id: str = ""

    # ---- 租户 ----
    tenant_id: str = "default"
    tenant_name: str = "我的店铺"

    # ---- 面板 / 回调 ----
    # 默认空 → 飞书按钮回调 **fail-closed**：未配置就直接拒绝。
    # 理由：一个能被随意伪造「老板已决定跟价」的端点，比没有这个端点危险得多。
    action_token_secret: str = ""
    panel_page_size: int = 200

    # ---- 调度 ----
    daily_brief_hour: int = 9
    daily_brief_minute: int = 0
    scheduler_enabled: bool = True
    collect_interval_minutes: int = 60      # 常规采集间隔
    blast_interval_minutes: int = 15        # 旺季/大促期间的加密间隔
    p0_push_enabled: bool = True            # P0 是否实时推送
    pool_maintain_weekday: int = 0          # 0=周一，重算分层
    pool_maintain_hour: int = 4

    @property
    def data_dir(self) -> Path:
        d = BASE_DIR / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
