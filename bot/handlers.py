import asyncio
import base64
import logging
import re

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
)
from aiogram.utils.chat_action import ChatActionSender

from bot.account import PROFILE_BUTTON, TOPUP_BUTTON, ensure_access, main_keyboard, subscribe_keyboard
from bot.billing import AI, FREE, IMAGE, PREMIUM, TRANSLATE, TRIAL, TRIAL_DAYS, VOICE, Billing, format_date
from bot.calculator import CalcError, calculate, looks_like_expression
from bot.documents import MAX_CHARS, MAX_FILE_SIZE, DocumentError, extract_text, is_supported
from bot.formatting import markdown_to_html, split_text
from bot.llm import LLMError, OpenRouterClient
from bot.memory import DialogMemory
from bot.speech import SpeechRecognizer

router = Router()
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — дружелюбный и умный помощник в Telegram. Отвечай на языке пользователя, по делу и понятно. "
    "Если просят решить задачу или пример — решай по шагам и в конце чётко пиши ответ. "
    "Не используй LaTeX: формулы пиши обычным текстом (например: x^2 + 3x = 0, √16 = 4, 3/4). "
    "Из форматирования используй только **жирный**, `код` и простые списки."
)

SOLVE_PROMPT = "Реши по шагам, объясняя каждый шаг простыми словами, и в конце выдели ответ:\n\n{task}"
ADVICE_PROMPT = "Дай практичный, конкретный и честный совет по ситуации. Предложи несколько вариантов действий:\n\n{task}"
PHOTO_DEFAULT_PROMPT = "Опиши, что на изображении. Если там задача или пример — реши по шагам."
TRANSLATE_RULES = (
    "Если пользователь указал язык (например, «на немецкий»), переводи на него. "
    "Иначе: текст на русском переводи на английский, текст на любом другом языке — на русский. "
    "Не добавляй пояснений."
)
TRANSLATE_PROMPT = "Переведи текст. " + TRANSLATE_RULES + " Выведи только перевод.\n\n{text}"
TRANSLATE_PHOTO_PROMPT = (
    "Найди весь текст на изображении и переведи его. " + TRANSLATE_RULES + " "
    "Сначала выведи исходный текст, затем перевод.{extra}"
)
DOCUMENT_DEFAULT_PROMPT = "Сделай краткое содержание: о чём документ, главные мысли и выводы."
DOCUMENT_PROMPT = "Текст файла «{name}»:\n\n{text}\n\n---\nЗадание пользователя: {task}"

MAX_VOICE_SECONDS = 180

IMAGE_REQUEST_RE = re.compile(
    r"^(?:нарисуй|сгенерируй\s+(?:картинку|изображение|фото)|создай\s+(?:картинку|изображение)|draw)[\s:,-]+(.+)",
    re.IGNORECASE | re.DOTALL,
)

HELP_TEXT = (
    "Я умею:\n\n"
    "💬 Отвечать на любые вопросы — просто напишите сообщение\n"
    "🎤 Понимать голосовые — запишите вопрос голосом, и я отвечу\n"
    "🧮 Считать примеры — напишите, например, 2+2*3 или /calc sqrt(16)+5^2\n"
    "📝 Решать задачи по шагам — /solve <задача>\n"
    "💡 Давать советы — /advice <ситуация>\n"
    "🌍 Переводить — /translate <текст>, фото с подписью /translate или ответ /translate на сообщение\n"
    "🎨 Рисовать картинки — /image <описание> или «нарисуй кота в космосе»\n"
    "📷 Разбирать фото — пришлите фото задачи, и я её решу\n"
    "📄 Читать файлы PDF, Word и TXT — краткое содержание и ответы по тексту (Премиум)\n"
    "🧹 Забыть разговор — /clear\n\n"
    f"{PROFILE_BUTTON} — баланс, тариф и остаток лимитов\n"
    f"{TOPUP_BUTTON} — пополнение звёздами и покупка подписки\n\n"
    "Калькулятор бесплатный и без лимитов. Он понимает: + - * / : ^ √, sqrt, sin, cos, tg, ln, lg, log, factorial, pi, e"
)


async def send_long(message: Message, text: str) -> None:
    for chunk in split_text(text):
        try:
            await message.answer(markdown_to_html(chunk), parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            await message.answer(chunk)


async def photo_content(bot: Bot, photo: PhotoSize, prompt: str) -> list[dict]:
    photo_file = await bot.download(photo)
    photo_b64 = base64.b64encode(photo_file.read()).decode()
    return [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{photo_b64}"}},
    ]


async def answer_with_ai(
    message: Message,
    bot: Bot,
    llm: OpenRouterClient,
    memory: DialogMemory,
    billing: Billing,
    content: str | list,
    history_text: str,
    kind: str | None = AI,
) -> None:
    """kind — какой лимит тратит запрос; None, если он уже списан (например, за голосовое)."""
    if kind and not await ensure_access(message, billing, kind):
        return

    chat_id = message.chat.id
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *memory.get(chat_id),
        {"role": "user", "content": content},
    ]
    async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
        try:
            reply = await llm.chat(messages)
        except LLMError as exc:
            await message.answer(f"⚠️ {exc}")
            return

    if not reply:
        await message.answer("Не получилось ответить, попробуйте переформулировать вопрос.")
        return

    if kind:
        billing.add_usage(message.from_user.id, kind)
    memory.add(chat_id, "user", history_text)
    memory.add(chat_id, "assistant", reply)
    await send_long(message, reply)


async def send_generated_image(
    message: Message, bot: Bot, llm: OpenRouterClient, billing: Billing, prompt: str
) -> None:
    if not await ensure_access(message, billing, IMAGE):
        return

    await message.answer("🎨 Рисую, это может занять до минуты...")
    async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
        try:
            image = await llm.generate_image(prompt)
        except LLMError as exc:
            await message.answer(f"⚠️ {exc}")
            return
        await message.answer_photo(BufferedInputFile(image, filename="image.png"), caption=prompt[:1024])
    billing.add_usage(message.from_user.id, IMAGE)


async def process_text(
    message: Message,
    bot: Bot,
    llm: OpenRouterClient,
    memory: DialogMemory,
    billing: Billing,
    text: str,
    kind: str | None = AI,
) -> None:
    """Общая обработка текста — и набранного, и распознанного из голосового."""
    if looks_like_expression(text):
        try:
            await message.answer(f"{text.rstrip('=?').strip()} = {calculate(text.rstrip('?'))}")
            return
        except CalcError:
            pass

    image_request = IMAGE_REQUEST_RE.match(text)
    if image_request:
        await send_generated_image(message, bot, llm, billing, image_request.group(1).strip())
        return

    await answer_with_ai(message, bot, llm, memory, billing, text, text, kind)


QUICK_START_HINTS = {
    "ask": (
        "💬 Просто напишите вопрос в чат или запишите голосовое — я отвечу.\n\n"
        "Например: «Почему небо голубое?»"
    ),
    "solve": (
        "🧮 Пришлите фото задачи или напишите её после /solve.\n"
        "Например: /solve 2x + 5 = 17\n\n"
        "Простые примеры вроде 2+2*3 я посчитаю сразу и бесплатно."
    ),
    "draw": (
        "🎨 Напишите /image и описание картинки или просто «нарисуй …».\n"
        "Например: нарисуй кота-астронавта на Луне"
    ),
    "translate": (
        "🌍 Напишите /translate и текст.\n"
        "Например: /translate Hello, how are you?\n\n"
        "Можно прислать фото с подписью /translate — переведу текст на картинке."
    ),
    "help": HELP_TEXT,
}


def quick_start_keyboard(with_subscription: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="💬 Задать вопрос", callback_data="start:ask"),
            InlineKeyboardButton(text="🧮 Решить задачу", callback_data="start:solve"),
        ],
        [
            InlineKeyboardButton(text="🎨 Нарисовать", callback_data="start:draw"),
            InlineKeyboardButton(text="🌍 Перевести", callback_data="start:translate"),
        ],
        [InlineKeyboardButton(text="❓ Что я умею", callback_data="start:help")],
    ]
    if with_subscription:
        rows.insert(0, [InlineKeyboardButton(text="💎 Купить подписку", callback_data="subs_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, billing: Billing) -> None:
    user = message.from_user
    is_new = not billing.user_exists(user.id)
    account = billing.get_account(user.id)
    bot_name = (await bot.me()).first_name

    if is_new:
        text = (
            f"Привет, {user.first_name}! 👋\n\n"
            f"Я {bot_name} — твой умный помощник. Отвечаю на вопросы, решаю задачи (даже по фото), "
            "понимаю голосовые, перевожу и рисую картинки.\n\n"
            f"🎁 Тебе подарен пробный период на {TRIAL_DAYS} дня: {TRIAL.daily_ai} ответов, "
            f"{TRIAL.daily_images} картинок, {TRIAL.voice} голосовых и {TRIAL.translate} перевода."
        )
    else:
        text = f"С возвращением, {user.first_name}! 👋\n\n"
        if account.plan is FREE:
            text += (
                "Пробный период закончился — бесплатно доступен калькулятор. "
                "Чтобы снова пользоваться всеми функциями, оформите подписку."
            )
        else:
            text += f"📦 Ваш тариф: {account.plan.title} до {format_date(account.plan_until)}."

    await message.answer(text, reply_markup=main_keyboard())
    await message.answer("С чего начнём? 👇", reply_markup=quick_start_keyboard(account.plan is FREE))


@router.callback_query(F.data.startswith("start:"))
async def on_quick_start(callback: CallbackQuery) -> None:
    hint = QUICK_START_HINTS.get(callback.data.removeprefix("start:"))
    await callback.answer()
    if hint:
        await callback.message.answer(hint)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_keyboard())


@router.message(Command("clear"))
async def cmd_clear(message: Message, memory: DialogMemory) -> None:
    memory.clear(message.chat.id)
    await message.answer("🧹 Готово, начинаем разговор с чистого листа.")


@router.message(Command("calc"))
async def cmd_calc(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Напишите пример после команды, например: /calc (2+3)^2 / 5")
        return
    try:
        await message.answer(f"{command.args.strip()} = {calculate(command.args)}")
    except CalcError as exc:
        await message.answer(f"Не получилось посчитать: {exc}. Попробуйте /solve для сложных задач.")


@router.message(Command("solve"))
async def cmd_solve(
    message: Message,
    command: CommandObject,
    bot: Bot,
    llm: OpenRouterClient,
    memory: DialogMemory,
    billing: Billing,
) -> None:
    if not command.args:
        await message.answer("Напишите задачу после команды, например: /solve 2x + 5 = 17")
        return
    await answer_with_ai(message, bot, llm, memory, billing, SOLVE_PROMPT.format(task=command.args), command.args)


@router.message(Command("advice"))
async def cmd_advice(
    message: Message,
    command: CommandObject,
    bot: Bot,
    llm: OpenRouterClient,
    memory: DialogMemory,
    billing: Billing,
) -> None:
    if not command.args:
        await message.answer("Опишите ситуацию после команды, например: /advice как подготовиться к экзамену за неделю")
        return
    await answer_with_ai(message, bot, llm, memory, billing, ADVICE_PROMPT.format(task=command.args), command.args)


@router.message(Command("translate"))
async def cmd_translate(
    message: Message,
    command: CommandObject,
    bot: Bot,
    llm: OpenRouterClient,
    memory: DialogMemory,
    billing: Billing,
) -> None:
    replied = message.reply_to_message
    instruction = (command.args or "").strip()
    photo = message.photo or (replied.photo if replied else None)

    if photo:
        if not await ensure_access(message, billing, TRANSLATE):
            return
        extra = f" Указание пользователя: {instruction}" if instruction else ""
        content = await photo_content(bot, photo[-1], TRANSLATE_PHOTO_PROMPT.format(extra=extra))
        await answer_with_ai(
            message, bot, llm, memory, billing, content, "[попросил перевести текст с фото]", TRANSLATE
        )
        return

    replied_text = (replied.text or replied.caption) if replied else None
    text = "\n\n".join(part for part in (instruction, replied_text) if part)
    if not text:
        await message.answer(
            "Как перевести:\n"
            "• /translate Hello, how are you?\n"
            "• /translate на немецкий: Привет, как дела?\n"
            "• пришлите фото с подписью /translate — переведу текст на картинке\n"
            "• ответьте командой /translate на любое сообщение\n\n"
            "По умолчанию русский переводится на английский, остальные языки — на русский."
        )
        return
    await answer_with_ai(
        message, bot, llm, memory, billing, TRANSLATE_PROMPT.format(text=text), f"Переведи: {text}", TRANSLATE
    )


@router.message(Command("image", "img"))
async def cmd_image(
    message: Message, command: CommandObject, bot: Bot, llm: OpenRouterClient, billing: Billing
) -> None:
    if not command.args:
        await message.answer("Опишите картинку после команды, например: /image кот-астронавт на Луне, мультяшный стиль")
        return
    await send_generated_image(message, bot, llm, billing, command.args)


@router.message(F.photo)
async def on_photo(
    message: Message, bot: Bot, llm: OpenRouterClient, memory: DialogMemory, billing: Billing
) -> None:
    if not await ensure_access(message, billing, AI):
        return
    question = message.caption or PHOTO_DEFAULT_PROMPT
    content = await photo_content(bot, message.photo[-1], question)
    await answer_with_ai(message, bot, llm, memory, billing, content, f"[прислал фото] {question}")


@router.message(F.voice | F.video_note | F.audio)
async def on_voice(
    message: Message,
    bot: Bot,
    llm: OpenRouterClient,
    memory: DialogMemory,
    billing: Billing,
    speech: SpeechRecognizer,
) -> None:
    media = message.voice or message.video_note or message.audio
    if media.duration and media.duration > MAX_VOICE_SECONDS:
        await message.answer(f"Запись слишком длинная. Пришлите голосовое до {MAX_VOICE_SECONDS // 60} минут.")
        return
    if not await ensure_access(message, billing, VOICE):
        return

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        audio = await bot.download(media)
        try:
            text = await speech.transcribe(audio)
        except Exception:
            logger.exception("Не удалось распознать голосовое сообщение")
            await message.answer("⚠️ Не получилось распознать запись. Попробуйте ещё раз или напишите текстом.")
            return

    if not text:
        await message.answer("🎤 Не расслышал слов в записи. Попробуйте сказать громче или ближе к микрофону.")
        return
    billing.add_usage(message.from_user.id, VOICE)
    await message.reply(f"🎤 {text}")
    await process_text(message, bot, llm, memory, billing, text, kind=None)


@router.message(F.document)
async def on_document(
    message: Message, bot: Bot, llm: OpenRouterClient, memory: DialogMemory, billing: Billing
) -> None:
    document = message.document
    if not billing.get_account(message.from_user.id).plan.files:
        await message.answer(
            f"📄 Чтение файлов доступно в подписке {PREMIUM.title}: краткое содержание PDF, Word и TXT "
            "и ответы на вопросы по тексту.",
            reply_markup=subscribe_keyboard(),
        )
        return
    if not is_supported(document.file_name):
        await message.answer("Я умею читать PDF, Word (.docx) и текстовые файлы (.txt). Картинки присылайте как фото.")
        return
    if document.file_size and document.file_size > MAX_FILE_SIZE:
        await message.answer(f"Файл слишком большой. Максимум — {MAX_FILE_SIZE // 1024 // 1024} МБ.")
        return
    if not await ensure_access(message, billing, AI):
        return

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        data = await bot.download(document)
        try:
            text, truncated = await asyncio.to_thread(extract_text, document.file_name, data.read())
        except DocumentError as exc:
            await message.answer(f"⚠️ {exc}")
            return

    if truncated:
        await message.answer(f"📄 Файл большой — я прочитал первые {MAX_CHARS:,} символов.".replace(",", " "))
    task = message.caption or DOCUMENT_DEFAULT_PROMPT
    content = DOCUMENT_PROMPT.format(name=document.file_name, text=text, task=task)
    await answer_with_ai(message, bot, llm, memory, billing, content, content)


@router.message(F.text)
async def on_text(
    message: Message, bot: Bot, llm: OpenRouterClient, memory: DialogMemory, billing: Billing
) -> None:
    text = message.text.strip()
    if text.startswith("/"):
        await message.answer("Не знаю такой команды. Напишите /help, чтобы узнать, что я умею.")
        return
    await process_text(message, bot, llm, memory, billing, text)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Такие сообщения я пока не понимаю. Напишите /help, чтобы узнать, что я умею.")
