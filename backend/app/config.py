from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=('.env', str(Path(__file__).resolve().parents[2] / '.env')), extra='ignore')
    app_name: str = 'KeyAcross'
    environment: str = 'production'
    database_url: str = 'postgresql+psycopg://keyacross:keyacross@localhost:5432/keyacross'
    redis_url: str = 'redis://localhost:6379/0'
    encryption_key: str = ''
    secret_key: str = ''
    bootstrap_username: str = ''
    bootstrap_password: str = ''
    cors_origins: str = 'http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080,http://127.0.0.1:8080'
    cookie_secure: bool = True
    session_idle_seconds: int = 7200
    session_max_seconds: int = 86400
    upload_limit: int = 100
    sync_interval_seconds: int = 300
    allowed_private_hosts: str = ''
    allow_http_sites: bool = False
    attachment_dir: str = './data/attachments'


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
