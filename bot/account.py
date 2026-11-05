import asyncio
import logging
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    User,
)

from bot.billing import (
    AI,
    FREE,
    IMAGE,
    PAID_PLANS,
    PREMIUM,
    PRO,
    SUBSCRIPTION_DAYS,
    TRANSLATE,
    VOICE,
    Account,
    Billing,
    Plan,
    PurchaseError,
    format_date,
    now,
    offerable_plans,
    plan_limit,
)

router = Router()
logger = logging.getLogger(__name__)

PROFILE_BUTTON = "👤 Профиль"
TOPUP_BUTTON = "💰 Пополнить баланс"

TOPUP_AMOUNTS = [40, 80, 150, 300, 500]
STARS_PREFIX = "stars:"

RENEWAL_OFFER_BEFORE = timedelta(days=1)
RENEWAL_OFFER_AFTER = timedelta(days=3)
RENEWAL_CHECK_INTERVAL = timedelta(minutes=30)

PAYSUPPORT_TEXT = (
    "Пополнение баланса звёздами не возвращается: звёзды остаются на балансе бота "
    "и тратятся на подписки.\n\n"
    "Платежи зачисляются автоматически — проверить баланс можно в разделе «👤 Профиль»."
)


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=PROFILE_BUTTON), KeyboardButton(text=TOPUP_BUTTON)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Купить подписку", callback_data="subs_menu")]])


def topup_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Пополнить звёздами", callback_data="stars_menu")],
            [InlineKeyboardButton(text="💎 Купить подписку", callback_data="subs_menu")],
        ]
    )


def stars_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=f"{amount} ⭐", callback_data=f"{STARS_PREFIX}{amount}") for amount in TOPUP_AMOUNTS
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[:3], buttons[3:], [InlineKeyboardButton(text="« Назад", callback_data="topup_menu")]]
    )


def subscriptions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *(
                [InlineKeyboardButton(text=f"Купить {plan.title} — {plan.price} ⭐", callback_data=f"buy:{plan.key}")]
                for plan in PAID_PLANS.values()
            ),
            [InlineKeyboardButton(text="« Назад", callback_data="topup_menu")],
        ]
    )


def describe_limit(plan: Plan, kind: str) -> str:
    limit = plan_limit(plan, kind)
    if limit is None:
        return "без лимита"
    return f"до {limit} за пробный период" if kind in plan.total_kinds else f"до {limit} в день"


def describe_plan(plan: Plan) -> str:
    lines = [
        f"• ответы, советы, решение задач и разбор фото — {describe_limit(plan, AI)}",
        f"• голосовые сообщения — {describe_limit(plan, VOICE)}",
        f"• перевод — {describe_limit(plan, TRANSLATE)}",
        f"• генерация картинок — {describe_limit(plan, IMAGE)}",
    ]
    if plan.files:
        lines.append("• чтение файлов PDF, Word и TXT")
    lines.append("• калькулятор — без лимита")
    return "\n".join(lines)


def parse_amount(data: str | None) -> int | None:
    if not data or not data.startswith(STARS_PREFIX):
        return None
    amount = data.removeprefix(STARS_PREFIX)
    if not amount.isdigit() or int(amount) not in TOPUP_AMOUNTS:
        return None
    return int(amount)


def profile_text(user: User, billing: Billing) -> str:
    account = billing.get_account(user.id)
    lines = [
        "👤 Профиль",
        "",
        f"Имя: {user.full_name}",
        f"ID: {user.id}",
        f"💰 Баланс: {account.balance} ⭐",
        "",
    ]

    if account.plan is FREE:
        lines.append("📦 Тариф: Бесплатный — доступен только калькулятор")
        return "\n".join(lines)

    lines.append(f"📦 Тариф: {account.plan.title} до {format_date(account.plan_until)}")
    lines.extend(usage_lines(billing, user.id, account.plan))
    return "\n".join(lines)


def usage_lines(billing: Billing, user_id: int, plan: Plan) -> list[str]:
    lines = []
    for icon, kind, title in (
        ("💬", AI, "Ответов"),
        ("🎤", VOICE, "Голосовых"),
        ("🌍", TRANSLATE, "Переводов"),
        ("🎨", IMAGE, "Картинок"),
    ):
        limit = plan_limit(plan, kind)
        if limit is None:
            lines.append(f"{icon} {title}: без лимита")
            continue
        period = "за пробный период" if kind in plan.total_kinds else "сегодня"
        lines.append(f"{icon} {title} {period}: {billing.get_usage(user_id, kind)} из {limit}")
    return lines


def subscriptions_text(balance: int) -> str:
    return (
        f"💎 Подписки на {SUBSCRIPTION_DAYS} дней\n\n"
        f"⭐ {PRO.title} — {PRO.price} ⭐\n{describe_plan(PRO)}\n\n"
        f"👑 {PREMIUM.title} — {PREMIUM.price} ⭐\n{describe_plan(PREMIUM)}\n\n"
        f"Ваш баланс: {balance} ⭐"
    )


async def edit_or_answer(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=reply_markup)


async def ensure_access(message: Message, billing: Billing, kind: str) -> bool:
    denial = billing.check_access(message.from_user.id, kind)
    if denial:
        await message.answer(denial, reply_markup=subscribe_keyboard())
        return False
    return True


@router.message(F.text == PROFILE_BUTTON)
@router.message(Command("profile"))
async def on_profile(message: Message, billing: Billing) -> None:
    await message.answer(profile_text(message.from_user, billing), reply_markup=topup_menu_keyboard())


@router.message(F.text == TOPUP_BUTTON)
@router.message(Command("topup"))
async def on_topup(message: Message, billing: Billing) -> None:
    balance = billing.get_account(message.from_user.id).balance
    await message.answer(f"💰 Ваш баланс: {balance} ⭐\n\nЧто хотите сделать?", reply_markup=topup_menu_keyboard())


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message) -> None:
    await message.answer(PAYSUPPORT_TEXT)


@router.callback_query(F.data == "topup_menu")
async def on_topup_menu(callback: CallbackQuery, billing: Billing) -> None:
    await callback.answer()
    balance = billing.get_account(callback.from_user.id).balance
    await edit_or_answer(callback, f"💰 Ваш баланс: {balance} ⭐\n\nЧто хотите сделать?", topup_menu_keyboard())


@router.callback_query(F.data == "stars_menu")
async def on_stars_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await edit_or_answer(
        callback,
        f"⭐ Выберите сумму пополнения.\n\nПодписка {PRO.title} стоит {PRO.price} ⭐, "
        f"{PREMIUM.title} — {PREMIUM.price} ⭐ в месяц.",
        stars_keyboard(),
    )


@router.callback_query(F.data.startswith(STARS_PREFIX))
async def on_amount_chosen(callback: CallbackQuery) -> None:
    amount = parse_amount(callback.data)
    if amount is None:
        await callback.answer("Неизвестная сумма", show_alert=True)
        return

    await callback.answer()
    await callback.message.answer_invoice(
        title="Пополнение баланса",
        description=f"{amount} ⭐ на баланс бота для покупки подписок. Пополнение не возвращается.",
        payload=f"{STARS_PREFIX}{amount}",
        currency="XTR",
        prices=[LabeledPrice(label="Пополнение баланса", amount=amount)],
    )


@router.callback_query(F.data == "subs_menu")
async def on_subscriptions_menu(callback: CallbackQuery, billing: Billing) -> None:
    await callback.answer()
    balance = billing.get_account(callback.from_user.id).balance
    await edit_or_answer(callback, subscriptions_text(balance), subscriptions_keyboard())


@router.callback_query(F.data.startswith("buy:"))
async def on_buy(callback: CallbackQuery, billing: Billing) -> None:
    plan = PAID_PLANS.get(callback.data.removeprefix("buy:"))
    if plan is None:
        await callback.answer("Неизвестная подписка", show_alert=True)
        return
    await callback.answer()

    account = billing.get_account(callback.from_user.id)
    if account.plan is PREMIUM and plan is PRO:
        await edit_or_answer(
            callback,
            f"У вас уже Премиум до {format_date(account.plan_until)}. Про можно будет купить после его окончания.",
            subscriptions_keyboard(),
        )
        return

    until = billing.subscription_start(account, plan) + timedelta(days=SUBSCRIPTION_DAYS)
    lines = [f"Купить подписку {plan.title} за {plan.price} ⭐?", "", describe_plan(plan), ""]
    if account.plan is plan:
        lines.append(f"Текущая подписка продлится до {format_date(until)}.")
    elif account.plan is PRO:
        lines.append(f"Премиум начнётся сразу и будет действовать до {format_date(until)}. Оставшиеся дни Про сгорят.")
    else:
        lines.append(f"Подписка начнётся сразу и будет действовать до {format_date(until)}.")
    lines.append(f"Ваш баланс: {account.balance} ⭐")

    await edit_or_answer(
        callback,
        "\n".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Купить", callback_data=f"confirm:{plan.key}")],
                [InlineKeyboardButton(text="« Назад", callback_data="subs_menu")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("confirm:"))
async def on_confirm_purchase(callback: CallbackQuery, billing: Billing) -> None:
    plan = PAID_PLANS.get(callback.data.removeprefix("confirm:"))
    if plan is None:
        await callback.answer("Неизвестная подписка", show_alert=True)
        return
    await callback.answer()

    try:
        account = billing.buy_plan(callback.from_user.id, plan)
    except PurchaseError as exc:
        buttons = [[InlineKeyboardButton(text="« Назад", callback_data="subs_menu")]]
        if exc.missing:
            buttons.insert(0, [InlineKeyboardButton(text="⭐ Пополнить баланс", callback_data="stars_menu")])
        await edit_or_answer(callback, f"❌ {exc}", InlineKeyboardMarkup(inline_keyboard=buttons))
        return

    logger.info("Пользователь %s купил %s до %s", callback.from_user.id, plan.key, account.plan_until)
    await edit_or_answer(
        callback,
        f"🎉 Подписка {plan.title} активна до {format_date(account.plan_until)}.\n"
        f"Остаток на балансе: {account.balance} ⭐",
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    amount = parse_amount(query.invoice_payload)
    if amount is None or query.total_amount != amount:
        await query.answer(ok=False, error_message="Этот счёт устарел, откройте «💰 Пополнить баланс» ещё раз.")
        return
    await query.answer(ok=True)


def purchase_offer(account: Account) -> tuple[str, InlineKeyboardMarkup]:
    """Вопрос «купить подписку?» с кнопками для подписок, на которые хватает баланса."""
    plans = offerable_plans(account)
    if not plans:
        cheapest = min(PAID_PLANS.values(), key=lambda plan: plan.price)
        text = f"Для подписки {cheapest.title} не хватает {cheapest.price - account.balance} ⭐."
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⭐ Пополнить ещё", callback_data="stars_menu")]]
        )
        return text, keyboard

    lines = ["Хотите купить подписку? Звёзды спишутся только после нажатия кнопки.", ""]
    buttons = []
    for plan in plans:
        until = Billing.subscription_start(account, plan) + timedelta(days=SUBSCRIPTION_DAYS)
        action = "Продлить" if plan is account.plan else "Купить"
        note = " (оставшиеся дни Про сгорят)" if account.plan is PRO and plan is PREMIUM else ""
        lines.append(
            f"• {plan.title} — {plan.price} ⭐: {plan.daily_ai} ответов, {plan.voice} голосовых "
            f"и {plan.daily_images} картинок в день, перевод без лимита, "
            f"до {format_date(until)}{note}"
        )
        buttons.append(
            [InlineKeyboardButton(text=f"✅ {action} {plan.title} — {plan.price} ⭐", callback_data=f"confirm:{plan.key}")]
        )
    buttons.append([InlineKeyboardButton(text="Нет, спасибо", callback_data="dismiss")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "dismiss")
async def on_dismiss(callback: CallbackQuery, billing: Billing) -> None:
    await callback.answer()
    balance = billing.get_account(callback.from_user.id).balance
    await edit_or_answer(
        callback,
        f"Хорошо! Звёзды остаются на балансе: {balance} ⭐\n"
        f"Купить подписку можно в любой момент через «{TOPUP_BUTTON}».",
    )


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, billing: Billing) -> None:
    payment = message.successful_payment
    credited = billing.credit_payment(message.from_user.id, payment.total_amount, payment.telegram_payment_charge_id)
    if credited:
        logger.info(
            "Пополнение %s ⭐ от %s (id=%s), charge_id=%s",
            payment.total_amount,
            message.from_user.username or message.from_user.full_name,
            message.from_user.id,
            payment.telegram_payment_charge_id,
        )
    account = billing.get_account(message.from_user.id)
    offer_text, offer_keyboard = purchase_offer(account)
    await message.answer(
        f"✅ Баланс пополнен на {payment.total_amount} ⭐\nТеперь на балансе: {account.balance} ⭐\n\n{offer_text}",
        reply_markup=offer_keyboard,
    )


async def send_renewal_offers(bot: Bot, billing: Billing) -> None:
    for account in billing.renewal_candidates(RENEWAL_OFFER_BEFORE, RENEWAL_OFFER_AFTER):
        if account.plan_until > now():
            status = f"⏰ Ваша подписка {account.plan.title} заканчивается {format_date(account.plan_until)}."
        else:
            status = f"⏰ Ваша подписка {account.plan.title} закончилась {format_date(account.plan_until)}."
        until = Billing.subscription_start(billing.get_account(account.user_id), account.plan) + timedelta(
            days=SUBSCRIPTION_DAYS
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🔄 Продлить за {account.plan.price} ⭐", callback_data=f"confirm:{account.plan.key}"
                    )
                ],
                [InlineKeyboardButton(text="Нет, спасибо", callback_data="dismiss")],
            ]
        )
        try:
            await bot.send_message(
                account.user_id,
                f"{status}\n\nНа балансе {account.balance} ⭐ — продлить подписку до {format_date(until)}? "
                "Звёзды спишутся только после нажатия кнопки.",
                reply_markup=keyboard,
            )
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning("Не удалось отправить предложение продления пользователю %s: %s", account.user_id, exc)
        billing.mark_renewal_offered(account.user_id, account.plan_until)


async def renewal_offer_loop(bot: Bot, billing: Billing) -> None:
    while True:
        try:
            await send_renewal_offers(bot, billing)
        except Exception:
            logger.exception("Ошибка при рассылке предложений продлить подписку")
        await asyncio.sleep(RENEWAL_CHECK_INTERVAL.total_seconds())
