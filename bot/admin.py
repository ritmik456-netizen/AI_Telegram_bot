import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from bot.account import usage_lines
from bot.billing import FREE, PAID_PLANS, SUBSCRIPTION_DAYS, Billing, format_date

router = Router()
logger = logging.getLogger(__name__)

ADMIN_COMMANDS = [
    BotCommand(command="admin", description="Админ: список команд"),
    BotCommand(command="stats", description="Админ: статистика"),
    BotCommand(command="user", description="Админ: информация о пользователе"),
    BotCommand(command="give", description="Админ: начислить звёзды"),
    BotCommand(command="grant", description="Админ: выдать подписку"),
    BotCommand(command="broadcast", description="Админ: рассылка всем"),
]

ADMIN_HELP = (
    "🛠 Админ-панель\n\n"
    "/stats — статистика бота\n"
    "/user <id> — баланс и тариф пользователя\n"
    "/give <id> <звёзды> — начислить звёзды (отрицательное число — списать)\n"
    f"/grant <id> <pro|premium> [дней] — выдать подписку (по умолчанию {SUBSCRIPTION_DAYS} дней)\n"
    "/broadcast <текст> — рассылка всем пользователям\n"
    "   или ответьте командой /broadcast на любое сообщение (фото, видео…), чтобы разослать его\n\n"
    "ID пользователя виден в его разделе «👤 Профиль»."
)

BROADCAST_DELAY_SECONDS = 0.05

pending_broadcasts: dict[int, tuple[int, int] | str] = {}


class IsAdmin(Filter):
    async def __call__(self, event: TelegramObject, admin_ids: frozenset[int]) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in admin_ids


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


def parse_user_id(raw: str, billing: Billing) -> int | None:
    if not raw.lstrip("-").isdigit():
        return None
    user_id = int(raw)
    return user_id if billing.user_exists(user_id) else None


async def notify(bot: Bot, user_id: int, text: str) -> bool:
    try:
        await bot.send_message(user_id, text)
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        return False


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    await message.answer(ADMIN_HELP)


@router.message(Command("stats"))
async def cmd_stats(message: Message, billing: Billing) -> None:
    s = billing.stats()
    await message.answer(
        "📊 Статистика\n\n"
        f"👥 Пользователей: {s['users']} (новых сегодня: {s['new_today']})\n"
        f"🎁 На пробном периоде: {s['trial']}\n"
        f"⭐ Про: {s['pro']}\n"
        f"👑 Премиум: {s['premium']}\n"
        f"🆓 Без подписки: {s['free']}\n\n"
        f"💰 Пополнений сегодня: {s['payments_today']} на {s['stars_today']} ⭐\n"
        f"💰 Пополнений всего: {s['payments_total']} на {s['stars_total']} ⭐\n"
        f"🏦 Звёзд на балансах пользователей: {s['balances']} ⭐\n\n"
        f"💬 Ответов нейросети сегодня: {s['ai_today']}\n"
        f"🌍 Переводов сегодня: {s['translate_today']}\n"
        f"🎨 Картинок сегодня: {s['image_today']}"
    )


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject, billing: Billing) -> None:
    user_id = parse_user_id((command.args or "").strip(), billing)
    if user_id is None:
        await message.answer("Использование: /user <id>. Пользователь должен хотя бы раз написать боту.")
        return
    account = billing.get_account(user_id)
    lines = [f"👤 Пользователь {user_id}", f"💰 Баланс: {account.balance} ⭐"]
    if account.plan is FREE:
        lines.append("📦 Тариф: Бесплатный")
    else:
        lines.append(f"📦 Тариф: {account.plan.title} до {format_date(account.plan_until)}")
        lines.extend(usage_lines(billing, user_id, account.plan))
    await message.answer("\n".join(lines))


@router.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject, bot: Bot, billing: Billing) -> None:
    args = (command.args or "").split()
    user_id = parse_user_id(args[0], billing) if len(args) == 2 else None
    if user_id is None or not args[1].lstrip("-").isdigit() or int(args[1]) == 0:
        await message.answer("Использование: /give <id> <звёзды>, например /give 123456789 50")
        return

    amount = int(args[1])
    balance = billing.add_balance(user_id, amount)
    logger.info("Админ %s изменил баланс %s на %s ⭐", message.from_user.id, user_id, amount)
    notified = True
    if amount > 0:
        notified = await notify(bot, user_id, f"🎁 Вам начислено {amount} ⭐ от администрации!\nБаланс: {balance} ⭐")
    await message.answer(
        f"✅ Баланс пользователя {user_id}: {balance} ⭐" + ("" if notified else "\n(уведомление не доставлено)")
    )


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, bot: Bot, billing: Billing) -> None:
    args = (command.args or "").split()
    user_id = parse_user_id(args[0], billing) if len(args) in (2, 3) else None
    plan = PAID_PLANS.get(args[1].lower()) if user_id is not None else None
    days = args[2] if len(args) == 3 else str(SUBSCRIPTION_DAYS)
    if user_id is None or plan is None or not days.isdigit() or int(days) == 0:
        await message.answer("Использование: /grant <id> <pro|premium> [дней], например /grant 123456789 premium 7")
        return

    account = billing.grant_plan(user_id, plan, int(days))
    logger.info("Админ %s выдал %s подписку %s на %s дн.", message.from_user.id, user_id, plan.key, days)
    notified = await notify(
        bot, user_id, f"🎁 Вам подарена подписка {plan.title} до {format_date(account.plan_until)}!"
    )
    await message.answer(
        f"✅ Пользователю {user_id} выдана подписка {plan.title} до {format_date(account.plan_until)}"
        + ("" if notified else "\n(уведомление не доставлено)")
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, billing: Billing) -> None:
    if message.reply_to_message:
        pending_broadcasts[message.from_user.id] = (message.chat.id, message.reply_to_message.message_id)
        preview = "сообщение, на которое вы ответили"
    elif command.args:
        pending_broadcasts[message.from_user.id] = command.args
        preview = f"текст:\n\n{command.args}"
    else:
        await message.answer(
            "Использование: /broadcast <текст> или ответьте командой /broadcast на сообщение, которое нужно разослать."
        )
        return

    total = len(billing.all_user_ids())
    await message.answer(
        f"📣 Разослать {total} пользователям {preview}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"✅ Отправить всем ({total})", callback_data="broadcast:send")],
                [InlineKeyboardButton(text="Отмена", callback_data="broadcast:cancel")],
            ]
        ),
    )


@router.callback_query(F.data == "broadcast:cancel")
async def on_broadcast_cancel(callback: CallbackQuery) -> None:
    pending_broadcasts.pop(callback.from_user.id, None)
    await callback.answer()
    await callback.message.edit_text("Рассылка отменена.")


@router.callback_query(F.data == "broadcast:send")
async def on_broadcast_send(callback: CallbackQuery, bot: Bot, billing: Billing) -> None:
    content = pending_broadcasts.pop(callback.from_user.id, None)
    await callback.answer()
    if content is None:
        await callback.message.edit_text("Эта рассылка уже отправлена или отменена.")
        return

    user_ids = billing.all_user_ids()
    await callback.message.edit_text(f"📣 Рассылка запущена: {len(user_ids)} пользователей...")
    delivered = 0
    for user_id in user_ids:
        for _ in range(2):
            try:
                if isinstance(content, str):
                    await bot.send_message(user_id, content)
                else:
                    await bot.copy_message(user_id, from_chat_id=content[0], message_id=content[1])
                delivered += 1
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
            except (TelegramForbiddenError, TelegramBadRequest):
                break
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    logger.info("Админ %s сделал рассылку: доставлено %s из %s", callback.from_user.id, delivered, len(user_ids))
    await callback.message.answer(
        f"✅ Рассылка завершена: доставлено {delivered} из {len(user_ids)}.\n"
        "Не доставлено тем, кто заблокировал бота."
    )
