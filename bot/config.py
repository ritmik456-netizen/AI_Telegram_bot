import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_TEXT_MODELS = "nvidia/nemotron-3.5-super-vl:free,nvidia/nemotron-3-ultra-550b-a55b:free"
DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent.parent / "bot.db"


@dataclass(frozen=True)
class Config:
    bot_token: str
    openrouter_api_key: str
    text_models: list[str]
    image_model: str
    history_limit: int
    database_path: str
    admin_ids: frozenset[int]


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните его.")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY не задан. Получите ключ на https://openrouter.ai/keys")

    return Config(
        bot_token=bot_token,
        openrouter_api_key=api_key,
        text_models=[model.strip() for model in os.getenv("TEXT_MODEL", DEFAULT_TEXT_MODELS).split(",") if model.strip()],
        image_model=os.getenv("IMAGE_MODEL", "google/gemini-3.1-flash-lite-image").strip(),
        history_limit=int(os.getenv("HISTORY_LIMIT", "20")),
        database_path=os.getenv("DATABASE_PATH", "").strip() or str(DEFAULT_DATABASE_PATH),
        admin_ids=frozenset(int(item) for item in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if item),
    )
