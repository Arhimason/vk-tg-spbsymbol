from pydantic_settings import BaseSettings, SettingsConfigDict

from vk_tg_bridge.models import Settings


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    vk_access_token: str = ""
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_bot_token: str = ""
    vk_refresh_token: str = ""
    vk_device_id: str = ""
    vk_client_id: str = ""
    telegram_session_path: str = "data/telegram.session"
    database_path: str = "data/bridge.db"


def load_settings(*, login_only: bool = False) -> Settings:
    env = EnvSettings()
    required = (
        ("TELEGRAM_API_ID", env.telegram_api_id),
        ("TELEGRAM_API_HASH", env.telegram_api_hash),
    )
    if not login_only:
        required = (
            ("VK_ACCESS_TOKEN", env.vk_access_token),
            *required,
            ("TELEGRAM_BOT_TOKEN", env.telegram_bot_token),
        )
    missing = [name for name, value in required if not value]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Не заданы переменные: {joined}")
    return Settings(
        vk_access_token=env.vk_access_token,
        telegram_api_id=env.telegram_api_id,
        telegram_api_hash=env.telegram_api_hash,
        telegram_bot_token=env.telegram_bot_token,
        vk_refresh_token=env.vk_refresh_token,
        vk_device_id=env.vk_device_id,
        vk_client_id=env.vk_client_id,
        telegram_session_path=env.telegram_session_path,
        database_path=env.database_path,
    )
