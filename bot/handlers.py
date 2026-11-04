import base64
import re

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message
from aiogram.utils.chat_action import ChatActionSender

from bot.calculator import CalcError, calculate, looks_like_expression
from bot.formatting import markdown_to_html, split_text
from bot.llm import LLMError, OpenRouterClient
from bot.memory import DialogMemory

router = Router()

SYSTEM_PROMPT = (
    "Ты — дружелюбный и умный помощник в Telegram. Отвечай на языке пользователя, по делу и понятно. "
    "Если просят решить задачу или пример — решай по шагам и в конце чётко пиши ответ. "
    "Не используй LaTeX: формулы пиши обычным текстом (например: x^2 + 3x = 0, √16 = 4, 3/4). "
    "Из форматирования используй только **жирный**, `код` и простые списки."
)

SOLVE_PROMPT = "Реши по шагам, объясняя каждый шаг простыми словами, и в конце выдели ответ:\n\n{task}"
ADVICE_PROMPT = "Дай практичный, конкретный и честный совет по ситуации. Предложи несколько вариантов действий:\n\n{task}"
PHOTO_DEFAULT_PROMPT = "Опиши, что на изображении. Если там задача или пример — реши по шагам."

IMAGE_REQUEST_RE = re.compile(
    r"^(?:нарисуй|сгенерируй\s+(?:картинку|изображение|фото)|создай\s+(?:картинку|изображение)|draw)[\s:,-]+(.+)",
    re.IGNORECASE | re.DOTALL,
)

HELP_TEXT = (
    "Я умею:\n\n"
    "💬 Отвечать на любые вопросы — просто напишите сообщение\n"
    "🧮 Считать примеры — напишите, например, 2+2*3 или /calc sqrt(16)+5^2\n"
    "📝 Решать задачи по шагам — /solve <задача>\n"
    "💡 Давать советы — /advice <ситуация>\n"
    "🎨 Рисовать картинки — /image <описание> или «нарисуй кота в космосе»\n"
    "📷 Разбирать фото — пришлите фото задачи, и я её решу\n"
    "🧹 Забыть разговор — /clear\n\n"
    "Калькулятор понимает: + - * / : ^ √, sqrt, sin, cos, tg, ln, lg, log, factorial, pi, e"
)


async def send_long(message: Message, text: str) -> None:
    for chunk in split_text(text):
        try:
            await message.answer(markdown_to_html(chunk), parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            await message.answer(chunk)


async def answer_with_ai(
    message: Message,
    bot: Bot,
    llm: OpenRouterClient,
    memory: DialogMemory,
    content: str | list,
    history_text: str,
) -> None:
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

    memory.add(chat_id, "user", history_text)
    memory.add(chat_id, "assistant", reply)
    await send_long(message, reply)


async def send_generated_image(message: Message, bot: Bot, llm: OpenRouterClient, prompt: str) -> None:
    await message.answer("🎨 Рисую, это может занять до минуты...")
    async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
        try:
            image = await llm.generate_image(prompt)
        except LLMError as exc:
            await message.answer(f"⚠️ {exc}")
            return
        await message.answer_photo(BufferedInputFile(image, filename="image.png"), caption=prompt[:1024])


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    name = message.from_user.first_name if message.from_user else "друг"
    await message.answer(f"Привет, {name}! 👋\n\n{HELP_TEXT}")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


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
    message: Message, command: CommandObject, bot: Bot, llm: OpenRouterClient, memory: DialogMemory
) -> None:
    if not command.args:
        await message.answer("Напишите задачу после команды, например: /solve 2x + 5 = 17")
        return
    await answer_with_ai(message, bot, llm, memory, SOLVE_PROMPT.format(task=command.args), command.args)


@router.message(Command("advice"))
async def cmd_advice(
    message: Message, command: CommandObject, bot: Bot, llm: OpenRouterClient, memory: DialogMemory
) -> None:
    if not command.args:
        await message.answer("Опишите ситуацию после команды, например: /advice как подготовиться к экзамену за неделю")
        return
    await answer_with_ai(message, bot, llm, memory, ADVICE_PROMPT.format(task=command.args), command.args)


@router.message(Command("image", "img"))
async def cmd_image(message: Message, command: CommandObject, bot: Bot, llm: OpenRouterClient) -> None:
    if not command.args:
        await message.answer("Опишите картинку после команды, например: /image кот-астронавт на Луне, мультяшный стиль")
        return
    await send_generated_image(message, bot, llm, command.args)


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot, llm: OpenRouterClient, memory: DialogMemory) -> None:
    photo_file = await bot.download(message.photo[-1])
    photo_b64 = base64.b64encode(photo_file.read()).decode()
    question = message.caption or PHOTO_DEFAULT_PROMPT
    content = [
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{photo_b64}"}},
    ]
    await answer_with_ai(message, bot, llm, memory, content, f"[прислал фото] {question}")


@router.message(F.text)
async def on_text(message: Message, bot: Bot, llm: OpenRouterClient, memory: DialogMemory) -> None:
    text = message.text.strip()

    if looks_like_expression(text):
        try:
            await message.answer(f"{text.rstrip('=?').strip()} = {calculate(text.rstrip('?'))}")
            return
        except CalcError:
            pass

    image_request = IMAGE_REQUEST_RE.match(text)
    if image_request:
        await send_generated_image(message, bot, llm, image_request.group(1).strip())
        return

    await answer_with_ai(message, bot, llm, memory, text, text)


@router.message()
async def on_other(message: Message) -> None:
    await message.answer("Пока я понимаю только текст и фото. Напишите /help, чтобы узнать, что я умею.")
