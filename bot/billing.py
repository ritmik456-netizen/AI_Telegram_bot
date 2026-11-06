import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

TRIAL_DAYS = 3
SUBSCRIPTION_DAYS = 30

AI = "ai"
IMAGE = "image"
TRANSLATE = "translate"

KIND_WORDS = {
    AI: ("ответ", "ответа", "ответов"),
    IMAGE: ("картинку", "картинки", "картинок"),
    TRANSLATE: ("перевод", "перевода", "переводов"),
}


def plural(count: int, forms: tuple[str, str, str]) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return forms[0]
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return forms[1]
    return forms[2]


@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    price: int
    daily_ai: int
    daily_images: int
    translate: int | None = 0
    """Переводов в день; None — без лимита."""
    files: bool = False
    total_kinds: frozenset[str] = frozenset()
    """Виды запросов, лимит которых действует на весь срок тарифа, а не на день."""


FREE = Plan("free", "Бесплатный", 0, 0, 0)
PRO = Plan("pro", "Про", 40, 25, 5, translate=None)
PREMIUM = Plan("premium", "Премиум", 80, 35, 15, translate=None, files=True)
TRIAL = Plan(
    "trial",
    "Пробный период",
    0,
    daily_ai=20,
    daily_images=5,
    translate=3,
    total_kinds=frozenset({AI, IMAGE, TRANSLATE}),
)
PAID_PLANS = {PRO.key: PRO, PREMIUM.key: PREMIUM}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    balance INTEGER NOT NULL DEFAULT 0,
    trial_until TEXT NOT NULL,
    plan TEXT,
    plan_until TEXT,
    renewal_offered_for TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    period TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, kind, period)
);
CREATE TABLE IF NOT EXISTS activity (
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind)
);
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Account:
    user_id: int
    balance: int
    plan: Plan
    plan_until: datetime | None


class PurchaseError(Exception):
    def __init__(self, message: str, missing: int = 0) -> None:
        super().__init__(message)
        self.missing = missing


def now() -> datetime:
    return datetime.now(MSK)


def format_date(value: datetime) -> str:
    return value.astimezone(MSK).strftime("%d.%m.%Y %H:%M") + " МСК"


def plan_limit(plan: Plan, kind: str) -> int | None:
    return {AI: plan.daily_ai, IMAGE: plan.daily_images, TRANSLATE: plan.translate}[kind]


def offerable_plans(account: Account) -> list[Plan]:
    """Подписки, на которые хватает баланса: сначала продление текущей, затем самая дорогая."""
    plans = [plan for plan in PAID_PLANS.values() if plan.price <= account.balance]
    if account.plan is PREMIUM:
        plans = [plan for plan in plans if plan is PREMIUM]
    return sorted(plans, key=lambda plan: (plan is not account.plan, -plan.price))


class Billing:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(users)")}
        if "renewal_offered_for" not in columns:
            self._db.execute("ALTER TABLE users ADD COLUMN renewal_offered_for TEXT")
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def get_account(self, user_id: int) -> Account:
        current = now()
        self._db.execute(
            "INSERT OR IGNORE INTO users (user_id, created_at, trial_until) VALUES (?, ?, ?)",
            (user_id, current.isoformat(), (current + timedelta(days=TRIAL_DAYS)).isoformat()),
        )
        self._db.commit()
        row = self._db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

        plan_until = datetime.fromisoformat(row["plan_until"]) if row["plan_until"] else None
        trial_until = datetime.fromisoformat(row["trial_until"])
        if row["plan"] in PAID_PLANS and plan_until and plan_until > current:
            return Account(user_id, row["balance"], PAID_PLANS[row["plan"]], plan_until)
        if trial_until > current:
            return Account(user_id, row["balance"], TRIAL, trial_until)
        return Account(user_id, row["balance"], FREE, None)

    def _period(self, user_id: int, kind: str) -> str:
        plan = self.get_account(user_id).plan
        return f"total:{plan.key}" if kind in plan.total_kinds else now().strftime("%Y-%m-%d")

    def get_usage(self, user_id: int, kind: str) -> int:
        row = self._db.execute(
            "SELECT count FROM usage WHERE user_id = ? AND kind = ? AND period = ?",
            (user_id, kind, self._period(user_id, kind)),
        ).fetchone()
        return row["count"] if row else 0

    def add_usage(self, user_id: int, kind: str) -> None:
        self._db.execute(
            "INSERT INTO usage (user_id, kind, period, count) VALUES (?, ?, ?, 1) "
            "ON CONFLICT (user_id, kind, period) DO UPDATE SET count = count + 1",
            (user_id, kind, self._period(user_id, kind)),
        )
        self._db.execute(
            "INSERT INTO activity (day, kind, count) VALUES (?, ?, 1) "
            "ON CONFLICT (day, kind) DO UPDATE SET count = count + 1",
            (now().strftime("%Y-%m-%d"), kind),
        )
        self._db.commit()

    def check_access(self, user_id: int, kind: str) -> str | None:
        """Возвращает текст отказа, если функция недоступна, иначе None."""
        account = self.get_account(user_id)
        if account.plan is FREE:
            return (
                "⏳ Пробный период закончился.\n\n"
                "Бесплатно доступен калькулятор (например, 2+2*3), а ответы нейросети, решение задач, "
                "советы, перевод и картинки — по подписке."
            )

        limit = plan_limit(account.plan, kind)
        if limit is None or self.get_usage(user_id, kind) < limit:
            return None

        what = plural(limit, KIND_WORDS[kind])
        if kind in account.plan.total_kinds:
            return f"📊 Вы использовали все {limit} {what}, доступных в пробном периоде. Оформите подписку, чтобы продолжить."
        upgrade_hint = "" if account.plan is PREMIUM else " В Премиуме лимиты больше."
        return f"📊 Вы использовали все {limit} {what} на сегодня. Лимит обновится завтра.{upgrade_hint}"

    def user_exists(self, user_id: int) -> bool:
        return self._db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is not None

    def all_user_ids(self) -> list[int]:
        return [row["user_id"] for row in self._db.execute("SELECT user_id FROM users")]

    def add_balance(self, user_id: int, amount: int) -> int:
        """Ручное начисление (или списание при отрицательной сумме) звёзд. Баланс не уходит в минус."""
        self._db.execute("UPDATE users SET balance = MAX(balance + ?, 0) WHERE user_id = ?", (amount, user_id))
        self._db.commit()
        return self.get_account(user_id).balance

    def grant_plan(self, user_id: int, plan: Plan, days: int) -> Account:
        """Выдаёт подписку бесплатно: продлевает текущую такую же или начинает новую с этого момента."""
        account = self.get_account(user_id)
        start = account.plan_until if account.plan is plan and account.plan_until else now()
        until = start + timedelta(days=days)
        self._db.execute(
            "UPDATE users SET plan = ?, plan_until = ? WHERE user_id = ?", (plan.key, until.isoformat(), user_id)
        )
        self._db.commit()
        return self.get_account(user_id)

    def stats(self) -> dict[str, int]:
        current = now()
        today = current.strftime("%Y-%m-%d")
        result = {
            "users": 0,
            "new_today": 0,
            "trial": 0,
            "free": 0,
            **{plan.key: 0 for plan in PAID_PLANS.values()},
        }
        for row in self._db.execute("SELECT created_at, trial_until, plan, plan_until FROM users"):
            result["users"] += 1
            if row["created_at"].startswith(today):
                result["new_today"] += 1
            if row["plan"] in PAID_PLANS and row["plan_until"] and datetime.fromisoformat(row["plan_until"]) > current:
                result[row["plan"]] += 1
            elif datetime.fromisoformat(row["trial_until"]) > current:
                result["trial"] += 1
            else:
                result["free"] += 1

        payments_today = self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments WHERE created_at LIKE ?", (f"{today}%",)
        ).fetchone()
        payments_total = self._db.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments").fetchone()
        result["payments_today"], result["stars_today"] = payments_today
        result["payments_total"], result["stars_total"] = payments_total
        result["balances"] = self._db.execute("SELECT COALESCE(SUM(balance), 0) FROM users").fetchone()[0]

        for kind in KIND_WORDS:
            result[f"{kind}_today"] = self._db.execute(
                "SELECT COALESCE(SUM(count), 0) FROM activity WHERE kind = ? AND day = ?", (kind, today)
            ).fetchone()[0]
        return result

    def renewal_candidates(self, before_end: timedelta, after_end: timedelta) -> list[Account]:
        """Подписки, которые скоро закончатся (или недавно закончились), если на продление хватает звёзд."""
        current = now()
        rows = self._db.execute(
            "SELECT user_id, balance, plan, plan_until, renewal_offered_for FROM users WHERE plan_until IS NOT NULL"
        ).fetchall()
        candidates = []
        for row in rows:
            plan = PAID_PLANS.get(row["plan"])
            plan_until = datetime.fromisoformat(row["plan_until"])
            if (
                plan
                and current - after_end <= plan_until <= current + before_end
                and row["balance"] >= plan.price
                and row["renewal_offered_for"] != row["plan_until"]
            ):
                candidates.append(Account(row["user_id"], row["balance"], plan, plan_until))
        return candidates

    def mark_renewal_offered(self, user_id: int, plan_until: datetime) -> None:
        self._db.execute(
            "UPDATE users SET renewal_offered_for = ? WHERE user_id = ?", (plan_until.isoformat(), user_id)
        )
        self._db.commit()

    def credit_payment(self, user_id: int, amount: int, charge_id: str) -> bool:
        """Зачисляет пополнение. Возвращает False, если этот платёж уже был зачислен."""
        self.get_account(user_id)
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO payments (charge_id, user_id, amount, created_at) VALUES (?, ?, ?, ?)",
            (charge_id, user_id, amount, now().isoformat()),
        )
        if cursor.rowcount == 0:
            return False
        self._db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        self._db.commit()
        return True

    @staticmethod
    def subscription_start(account: Account, plan: Plan) -> datetime:
        """Продление той же подписки добавляет месяц к сроку, остальные покупки начинаются сразу."""
        if account.plan_until and account.plan is plan:
            return account.plan_until
        return now()

    def buy_plan(self, user_id: int, plan: Plan) -> Account:
        account = self.get_account(user_id)
        if account.plan is PREMIUM and plan is PRO:
            raise PurchaseError(
                f"У вас уже Премиум до {format_date(account.plan_until)}. "
                "Про можно будет купить после его окончания."
            )
        if account.balance < plan.price:
            missing = plan.price - account.balance
            raise PurchaseError(f"Не хватает {missing} ⭐ на балансе.", missing=missing)

        until = self.subscription_start(account, plan) + timedelta(days=SUBSCRIPTION_DAYS)
        cursor = self._db.execute(
            "UPDATE users SET balance = balance - ?, plan = ?, plan_until = ? WHERE user_id = ? AND balance >= ?",
            (plan.price, plan.key, until.isoformat(), user_id, plan.price),
        )
        self._db.commit()
        if cursor.rowcount == 0:
            raise PurchaseError("Не хватает звёзд на балансе.", missing=plan.price)
        return self.get_account(user_id)
