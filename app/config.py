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
