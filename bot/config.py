from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    admin_ids: str = ""
    secret_key: str
    db_path: str = "data/bot.db"
    bot_title: str = "OpenVPN Creator"

    # Таймауты SSH (секунды)
    install_step_timeout: int = 1800
    key_cmd_timeout: int = 300

    @field_validator("secret_key")
    @classmethod
    def _secret_not_default(cls, v: str) -> str:
        if len(v) < 16:
            raise ValueError("SECRET_KEY должен быть не короче 16 символов")
        return v

    @property
    def admins(self) -> set[int]:
        return {int(x) for x in self.admin_ids.replace(" ", "").split(",") if x}


config = Config()
