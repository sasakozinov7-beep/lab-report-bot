import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Telegram
    BOT_TOKEN: str = ""

    # LLM Settings
    LLM_PROVIDER: str = "openai"  # 'openai' or 'gemini'
    
    # OpenAI / Compatible API
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = "gpt-4o"

    # Gemini API
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.8-flash"

    # Timeouts & Limits
    RUNNER_TIMEOUT_SECONDS: int = 90
    MAX_PROMPT_CHARS: int = 35000

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(__file__), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

config = Settings()
