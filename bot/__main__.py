import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from bot.config import load_config
from bot.handlers import router
from bot.llm import OpenRouterClient
from bot.memory import DialogMemory

COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="help", description="Что умеет бот"),
    BotCommand(command="solve", description="Решить задачу по шагам"),
    BotCommand(command="calc", description="Посчитать пример"),
    BotCommand(command="advice", description="Получить совет"),
    BotCommand(command="image", description="Сгенерировать картинку"),
    BotCommand(command="clear", description="Очистить историю диалога"),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()

    bot = Bot(config.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    llm = OpenRouterClient(config.openrouter_api_key, config.text_models, config.image_model)
    memory = DialogMemory(config.history_limit)

    await bot.set_my_commands(COMMANDS)
    logging.info(
        "Бот запущен. Текстовые модели (по порядку): %s, модель картинок: %s",
        ", ".join(config.text_models),
        config.image_model,
    )
    try:
        await dispatcher.start_polling(bot, llm=llm, memory=memory)
    finally:
        await llm.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
