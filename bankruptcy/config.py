"""Configuration loaded from environment variables / .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    courtlistener_api_token: str
    database_url: str
    alert_webhook_url: str = ""

    @property
    def sqlalchemy_database_url(self) -> str:
        """SQLAlchemy needs the +psycopg dialect suffix to use psycopg3.

        Supabase / standard Postgres URLs come as `postgresql://...`. Without
        the suffix, SQLAlchemy defaults to psycopg2, which we don't install.
        """
        url = self.database_url
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url.removeprefix("postgresql://")
        return url


settings = Settings()
