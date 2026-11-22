from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str
    secret_key: str
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 14
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    auth_rate_limit_requests: int = 10
    auth_rate_limit_window_seconds: int = 60
    login_account_rate_limit_attempts: int = 5
    login_account_rate_limit_window_seconds: int = 900
    max_profile_image_bytes: int = 5 * 1024 * 1024

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
