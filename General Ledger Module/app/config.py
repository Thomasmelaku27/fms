from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FMS_", env_file=".env", extra="ignore")

    app_name: str = "Financial Management System"
    database_url: str = "sqlite:///./fms.db"
    debug: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
