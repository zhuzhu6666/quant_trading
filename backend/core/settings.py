"""Backend settings — wraps QUANT_* env vars with sensible defaults."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QUANT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_root: str = "."
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    web_port: int = 3000
    db_path: str = "data/ctrader_data.duckdb"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Singleton accessor."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
