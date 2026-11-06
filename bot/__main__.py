import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeChat

from bot.account import renewal_offer_loop
from bot.account import router as account_router
from bot.admin import ADMIN_COMMANDS
from bot.admin import router as admin_router
from bot.antispam import AntiSpamMiddleware
from bot.billing import TRIAL_DAYS, Billing
from bot.config import load_config
from bot.handlers import router
from bot.llm import OpenRouterClient
from bot.memory import DialogMemory

COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="help", description="Что умеет бот"),
    BotCommand(command="profile", description="Профиль и баланс"),
    BotCommand(command="topup", description="Пополнить баланс и купить подписку"),
    BotCommand(command="solve", description="Решить задачу по шагам"),
    BotCommand(command="translate", description="Перевести текст или фото"),
    BotCommand(command="calc", description="Посчитать пример"),
    BotCommand(command="advice", description="Получить совет"),
    BotCommand(command="image", description="Сгенерировать картинку"),
    BotCommand(command="clear", description="Очистить историю диалога"),
    BotCommand(command="paysupport", description="Вопросы по оплате"),
]


def bot_description(name: str) -> str:
    return (
        f"🤖 {name} — твой умный помощник в Telegram.\n\n"
        "💬 Отвечает на любые вопросы\n"
        "🧮 Решает задачи и примеры, в том числе по фото\n"
        "🌍 Переводит текст и фото\n"
        "🎨 Рисует картинки\n"
        "📄 Читает PDF и Word\n\n"
        f"🎁 {TRIAL_DAYS} дня пробного периода — бесплатно! Нажмите «Старт»."
    )


SHORT_DESCRIPTION = (
    "Умный помощник: ответы на вопросы, решение задач по фото, перевод и картинки. "
    f"{TRIAL_DAYS} дня бесплатно!"
)


async def set_profile(bot: Bot) -> None:
    try:
        name = (await bot.me()).first_name
        await bot.set_my_description(bot_description(name))
        await bot.set_my_short_description(SHORT_DESCRIPTION)
    except TelegramBadRequest as exc:
        logging.warning("Не удалось обновить описание бота: %s", exc)


async def set_commands(bot: Bot, admin_ids: frozenset[int]) -> None:
    await bot.set_my_commands(COMMANDS)
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(COMMANDS + ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramBadRequest:
            logging.warning("Не удалось показать админ-команды пользователю %s: он ещё не писал боту", admin_id)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()

    bot = Bot(config.bot_token)
    dispatcher = Dispatcher()
    dispatcher.message.outer_middleware(AntiSpamMiddleware())
    dispatcher.include_router(admin_router)
    dispatcher.include_router(account_router)
    dispatcher.include_router(router)

    llm = OpenRouterClient(config.openrouter_api_key, config.text_models, config.image_model)
    memory = DialogMemory(config.history_limit)
    billing = Billing(config.database_path)

    await set_commands(bot, config.admin_ids)
    await set_profile(bot)
    if not config.admin_ids:
        logging.warning("ADMIN_IDS не задан в .env — админ-панель недоступна")
    logging.info(
        "Бот запущен. Текстовые модели (по порядку): %s, модель картинок: %s, база: %s, админы: %s",
        ", ".join(config.text_models),
        config.image_model,
        config.database_path,
        ", ".join(map(str, config.admin_ids)) or "нет",
    )
    renewal_task = asyncio.create_task(renewal_offer_loop(bot, billing))
    try:
        await dispatcher.start_polling(bot, llm=llm, memory=memory, billing=billing, admin_ids=config.admin_ids)
    finally:
        renewal_task.cancel()
        billing.close()
        await llm.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
