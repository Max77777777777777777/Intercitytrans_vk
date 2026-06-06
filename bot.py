# ══════════════════════════════════════════════════════════════
#  МЕЖГОРОД ТРАНСФЕР — ВКонтакте бот v1.0
#  vkbottle + asyncpg + общая PostgreSQL БД с ТГ ботом
# ══════════════════════════════════════════════════════════════

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone

import asyncpg
from vkbottle import Bot, Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Message

# ══════════════ КОНФИГ ══════════════
VK_TOKEN        = os.getenv("VK_TOKEN", "")
ADMIN_VK_IDS    = [int(x) for x in os.getenv("ADMIN_VK_IDS", "243400786").split(",") if x]
PG_DSN          = os.getenv("PG_DSN", "")
DATA_DIR        = os.getenv("DATA_DIR", "/app/data")
PAYMENT_DETAILS = os.getenv("PAYMENT_DETAILS", "Для оплаты абонемента свяжитесь с администратором")
TZ              = timezone(timedelta(hours=3))
TRIAL_DAYS      = 50

os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, "vk_bot.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ══════════════ ТАРИФЫ ══════════════
TARIFF_DATA = {
    "standard": ("🚗 Стандарт",            "от 2008 г.", 25, 40),
    "comfort":  ("🚙 Комфорт",              "от 2015 г.", 34, 50),
    "comfort+": ("✨ Комфорт+",             "от 2019 г.", 40, 58),
    "minivan":  ("🚐 Минивэн / Компактвэн", "",           45, 65),
    "business": ("💼 Бизнес",               "от 2018 г.", 60, 80),
}
NT_KW = ["лнр","днр","луганск","донецк","крым","симферополь","севастополь",
          "херсон","запорожье","мариуполь","мелитополь"]
SUBS = {
    "60":  {"days": 60,  "price": 650,  "label": "60 дней — 650 ₽"},
    "120": {"days": 120, "price": 1100, "label": "120 дней — 1 100 ₽"},
    "240": {"days": 240, "price": 2000, "label": "240 дней — 2 000 ₽"},
    "365": {"days": 365, "price": 3500, "label": "1 год — 3 500 ₽"},
}
DIST_COEFF = 1.25

def _tariff_dict(nt=False):
    idx = 3 if nt else 2
    return {k: {"label": v[0], "year": v[1], "price": v[idx]} for k, v in TARIFF_DATA.items()}

STATUS_ICON = {
    "open":      "🟢 Открыт",
    "taken":     "🔵 Принят",
    "completed": "✅ Завершён",
    "cancelled": "🔴 Отменён",
    "pending":   "⏳ Расчёт...",
}

# ══════════════ УТИЛИТЫ ══════════════
def now_dt(): return datetime.now(TZ)
def now_iso(): return now_dt().isoformat()

def fmt_order(o):
    nt  = any(kw in (o.get("from_city","") + o.get("to_city","")).lower() for kw in NT_KW)
    t   = _tariff_dict(nt).get(o.get("car_class","standard"), {})
    dkm = o.get("distance_km")
    pr  = o.get("price")
    dist_str  = f"{int(dkm)} км" if dkm else "⚠️ Не рассчитано"
    price_str = f"{t.get('price',0)} ₽/км | 💰 {pr} ₽" if pr else "Стоимость уточняется"
    st  = STATUS_ICON.get(o.get("status",""), o.get("status",""))
    return (
        f"🚖 Заказ #{o['id']} · 🇷🇺 РФ\n"
        f"📍 {o.get('from_city')} → {o.get('to_city')}\n"
        f"📐 {dist_str} | 📅 {o.get('trip_date')} | 🕐 {o.get('trip_time')}\n"
        f"👥 {o.get('passengers')} чел. | {t.get('label','')}\n"
        f"💵 {price_str}\n"
        f"⚠️ Платные дороги — отдельно\n"
        f"📌 {st}"
    )

def calc_price(dkm, car_class, from_city, to_city):
    nt = any(kw in (from_city+to_city).lower() for kw in NT_KW)
    t  = _tariff_dict(nt).get(car_class, _tariff_dict(nt)["standard"])
    return round(dkm * t["price"])

def get_distance(from_city, to_city):
    try:
        from geopy.geocoders import Yandex
        from geopy.distance import geodesic
        key = os.getenv("YANDEX_GEOCODER_KEY","")
        if not key: return None
        geo = Yandex(api_key=key)
        l1 = geo.geocode(from_city, exactly_one=True, timeout=10)
        l2 = geo.geocode(to_city,   exactly_one=True, timeout=10)
        if not l1 or not l2: return None
        return geodesic((l1.latitude,l1.longitude),(l2.latitude,l2.longitude)).km
    except Exception as e:
        log.error(f"Геокодирование: {e}"); return None

# ══════════════ БД ══════════════
_pg_pool: asyncpg.Pool = None

async def _init_pool():
    global _pg_pool
    _pg_pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=10, command_timeout=10)
    log.info("✅ Пул соединений PostgreSQL создан (asyncpg)")

class DB:
    @staticmethod
    async def driver(uid):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("SELECT * FROM drivers WHERE user_id=$1", uid)
        return dict(r) if r else None

    @staticmethod
    async def driver_save(uid, d):
        async with _pg_pool.acquire() as c:
            await c.execute(
                "INSERT INTO drivers VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "name=EXCLUDED.name, first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name, "
                "car_model=EXCLUDED.car_model, car_year=EXCLUDED.car_year, car_number=EXCLUDED.car_number, "
                "car_class=EXCLUDED.car_class, car_class_label=EXCLUDED.car_class_label, "
                "phone=EXCLUDED.phone, username=EXCLUDED.username, profile_link=EXCLUDED.profile_link, "
                "has_photo=EXCLUDED.has_photo, docs_verified=EXCLUDED.docs_verified",
                uid, d.get("name"), d.get("first_name"), d.get("last_name"),
                d.get("car_model"), d.get("car_year"), d.get("car_number"),
                d.get("car_class"), d.get("car_class_label"), d.get("phone"),
                d.get("username"), d.get("profile_link"),
                0, 0, d.get("registered_at", now_iso())
            )

    @staticmethod
    async def driver_verify(uid, v=True):
        async with _pg_pool.acquire() as c:
            await c.execute("UPDATE drivers SET docs_verified=$1 WHERE user_id=$2", 1 if v else 0, uid)

    @staticmethod
    async def all_drivers():
        async with _pg_pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM drivers ORDER BY registered_at DESC")
        return [dict(r) for r in rows]

    @staticmethod
    async def active_drivers():
        today = now_dt().date().strftime("%Y-%m-%d")
        async with _pg_pool.acquire() as c:
            rows = await c.fetch("""
                SELECT d.* FROM drivers d
                JOIN subscriptions s ON s.user_id=d.user_id
                WHERE d.docs_verified=1 AND s.expires_date>=$1
                AND d.user_id NOT IN (SELECT user_id FROM blacklist)
            """, today)
        return [dict(r) for r in rows]

    @staticmethod
    async def sub_info(uid):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("SELECT expires_date FROM subscriptions WHERE user_id=$1", uid)
        if not r: return None, 0, False
        try:
            exp  = datetime.strptime(r["expires_date"], "%Y-%m-%d").date()
            days = max(0, (exp - now_dt().date()).days)
            return r["expires_date"], days, days > 0
        except:
            return r["expires_date"], 0, False

    @staticmethod
    async def sub_set(uid, exp):
        async with _pg_pool.acquire() as c:
            await c.execute(
                "INSERT INTO subscriptions VALUES ($1,$2) "
                "ON CONFLICT (user_id) DO UPDATE SET expires_date=EXCLUDED.expires_date",
                uid, exp)

    @staticmethod
    async def order_create(data):
        async with _pg_pool.acquire() as c:
            row = await c.fetchrow(
                "INSERT INTO orders (passenger_id,from_city,to_city,trip_date,trip_time,"
                "passengers,car_class,wishes,distance_km,price,status,created_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id",
                data["passenger_id"], data["from_city"], data["to_city"],
                data["trip_date"], data["trip_time"], data["passengers"],
                data["car_class"], data.get("wishes"), data.get("distance_km"),
                data.get("price"), data.get("status","pending"), now_iso()
            )
        return row["id"]

    @staticmethod
    async def order(oid):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("SELECT * FROM orders WHERE id=$1", oid)
        return dict(r) if r else None

    @staticmethod
    async def order_upd(oid, **kw):
        if not kw: return
        i, sets, vals = 1, [], []
        for k, v in kw.items():
            sets.append(f"{k}=${i}"); vals.append(v); i += 1
        vals.append(oid)
        async with _pg_pool.acquire() as c:
            await c.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=${i}", *vals)

    @staticmethod
    async def order_cancel_atomic(oid, uid, role="passenger"):
        async with _pg_pool.acquire() as c:
            if role == "passenger":
                result = await c.execute(
                    "UPDATE orders SET status='cancelled' WHERE id=$1 AND passenger_id=$2 "
                    "AND status IN ('open','taken','pending')", oid, uid)
            else:
                result = await c.execute(
                    "UPDATE orders SET status='open',driver_id=NULL,taken_at=NULL "
                    "WHERE id=$1 AND driver_id=$2 AND status='taken'", oid, uid)
        return int(result.split()[-1]) > 0

    @staticmethod
    async def order_take_atomic(oid, uid):
        async with _pg_pool.acquire() as c:
            result = await c.execute(
                "UPDATE orders SET status='taken',driver_id=$1,taken_at=$2 "
                "WHERE id=$3 AND status='open' AND passenger_id!=$1",
                uid, now_iso(), oid)
        return int(result.split()[-1]) > 0

    @staticmethod
    async def passenger_orders(uid, limit=5):
        async with _pg_pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM orders WHERE passenger_id=$1 ORDER BY created_at DESC LIMIT $2",
                uid, limit)
        return [dict(r) for r in rows]

    @staticmethod
    async def open_orders(limit=10):
        async with _pg_pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM orders WHERE status='open' ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    @staticmethod
    async def all_orders(limit=10, status=None):
        async with _pg_pool.acquire() as c:
            if status:
                rows = await c.fetch(
                    "SELECT * FROM orders WHERE status=$1 ORDER BY created_at DESC LIMIT $2",
                    status, limit)
            else:
                rows = await c.fetch(
                    "SELECT * FROM orders ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    @staticmethod
    async def stats():
        today = now_dt().date().strftime("%Y-%m-%d")
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM orders) as t,
                    (SELECT COUNT(*) FROM orders WHERE status='open') as o,
                    (SELECT COUNT(*) FROM orders WHERE status='completed') as d,
                    (SELECT COUNT(*) FROM drivers) as dr,
                    (SELECT COUNT(*) FROM drivers WHERE docs_verified=1) as dc,
                    (SELECT COUNT(*) FROM drivers d
                     JOIN subscriptions s ON s.user_id=d.user_id
                     WHERE s.expires_date>=$1) as sub
            """, today)
        return {"total": r["t"], "open": r["o"], "done": r["d"],
                "drivers": r["dr"], "docs_ok": r["dc"], "subscribed": r["sub"]}

    @staticmethod
    async def bl_check(uid):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("SELECT 1 FROM blacklist WHERE user_id=$1", uid)
        return r is not None

    @staticmethod
    async def bl_add(uid):
        async with _pg_pool.acquire() as c:
            await c.execute("INSERT INTO blacklist VALUES ($1) ON CONFLICT DO NOTHING", uid)

    @staticmethod
    async def bl_remove(uid):
        async with _pg_pool.acquire() as c:
            await c.execute("DELETE FROM blacklist WHERE user_id=$1", uid)

    @staticmethod
    async def pending_set(uid, pk):
        async with _pg_pool.acquire() as c:
            await c.execute(
                "INSERT INTO pending_subscriptions VALUES ($1,$2) "
                "ON CONFLICT (user_id) DO UPDATE SET plan_key=EXCLUDED.plan_key", uid, pk)

    @staticmethod
    async def pending_get(uid):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("SELECT plan_key FROM pending_subscriptions WHERE user_id=$1", uid)
        return r["plan_key"] if r else None

    @staticmethod
    async def pending_del(uid, admin_id=None, plan_key=None):
        async with _pg_pool.acquire() as c:
            await c.execute("DELETE FROM pending_subscriptions WHERE user_id=$1", uid)
            if admin_id and plan_key:
                await c.execute(
                    "INSERT INTO subscription_log (user_id,plan_key,admin_id,action,created_at) "
                    "VALUES ($1,$2,$3,$4,$5)", uid, plan_key, admin_id, "reject_vk", now_iso())

# ══════════════ FSM (состояния в памяти) ══════════════
_STATES = {}  # uid -> state
_DATA   = {}  # uid -> dict

def get_state(uid): return _STATES.get(uid)
def set_state(uid, s): _STATES[uid] = s
def clear_state(uid): _STATES.pop(uid, None); _DATA.pop(uid, None)
def get_data(uid): return _DATA.get(uid, {})
def update_data(uid, **kw): _DATA.setdefault(uid, {}).update(kw)

class S:
    # Заказ
    from_city  = "order_from_city"
    to_city    = "order_to_city"
    trip_date  = "order_trip_date"
    trip_time  = "order_trip_time"
    passengers = "order_passengers"
    car_class  = "order_car_class"
    wishes     = "order_wishes"
    # Водитель
    first_name = "drv_first_name"
    last_name  = "drv_last_name"
    phone      = "drv_phone"
    car_model  = "drv_car_model"
    car_year   = "drv_car_year"
    car_number = "drv_car_number"
    car_class  = "drv_car_class"
    # Админ
    sub_uid    = "adm_sub_uid"
    sub_days   = "adm_sub_days"
    ban_uid    = "adm_ban_uid"

# ══════════════ КЛАВИАТУРЫ ══════════════
def kb_main():
    kb = Keyboard(one_time=False)
    kb.add(Text("🚕 Создать заказ"), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text("📋 Мои заказы"))
    kb.add(Text("📊 Тарифы"))
    return kb.get_json()

def kb_driver_main():
    kb = Keyboard(one_time=False)
    kb.add(Text("📦 Доступные заказы"), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text("👤 Мой профиль"))
    kb.add(Text("💳 Абонемент"))
    kb.row()
    kb.add(Text("🚕 Создать заказ"))
    kb.add(Text("📋 Мои заказы"))
    kb.row()
    kb.add(Text("📊 Тарифы"))
    return kb.get_json()

def kb_admin():
    kb = Keyboard(one_time=False)
    kb.add(Text("👥 Водители"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("📋 Все заказы"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("📊 Статистика"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("💳 Выдать подписку"), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text("🚫 Забанить"), color=KeyboardButtonColor.NEGATIVE)
    kb.add(Text("✅ Разбанить"), color=KeyboardButtonColor.POSITIVE)
    kb.row()
    kb.add(Text("🔙 Главное меню"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()

def kb_cancel():
    kb = Keyboard(one_time=True)
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_yes_no():
    kb = Keyboard(one_time=True)
    kb.add(Text("Нет"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_car_class():
    kb = Keyboard(one_time=True)
    for k, v in TARIFF_DATA.items():
        kb.add(Text(v[0]))
        kb.row()
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_passengers():
    kb = Keyboard(one_time=True)
    for i in range(1, 7):
        kb.add(Text(str(i)))
    kb.row()
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_subs():
    kb = Keyboard(one_time=True)
    for k, v in SUBS.items():
        kb.add(Text(v["label"]))
        kb.row()
    kb.add(Text("🔙 Назад"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()

# ══════════════ БОТ ══════════════
bot = Bot(token=VK_TOKEN)
_BL_CACHE: set = set()

async def send(uid, text, keyboard=None):
    try:
        await bot.api.messages.send(
            user_id=uid, message=str(text)[:4096],
            keyboard=keyboard, random_id=0)
    except Exception as e:
        log.error(f"Ошибка отправки {uid}: {e}")

# ══════════════ ОБРАБОТЧИК ══════════════
@bot.on.message()
async def handler(message: Message):
    uid  = message.from_id
    text = (message.text or "").strip()

    # Чёрный список
    if uid in _BL_CACHE:
        return

    state  = get_state(uid)
    drv    = await DB.driver(uid)
    is_adm = uid in ADMIN_VK_IDS

    # ── СТАРТ ──
    if text.lower() in ["начать", "start", "/start", "привет", "меню", "помощь"]:
        clear_state(uid)
        if is_adm:
            await send(uid, "👋 Добро пожаловать, администратор!", keyboard=kb_admin())
        elif drv:
            await send(uid, f"👋 С возвращением, {drv.get('first_name','')}!", keyboard=kb_driver_main())
        else:
            await send(uid, "👋 Добро пожаловать в Межгород Трансфер!\n\nВыберите действие:", keyboard=kb_main())
        return

    # ── ОТМЕНА ──
    if text == "❌ Отменить":
        clear_state(uid)
        kb = kb_driver_main() if drv else kb_main()
        await send(uid, "Отменено.", keyboard=kb)
        return

    # ── ГЛАВНОЕ МЕНЮ ──
    if text == "📊 Тарифы":
        lines = ["📊 Тарифы:\n"]
        for k, v in TARIFF_DATA.items():
            lines.append(f"{v[0]}: {v[2]} ₽/км")
        await send(uid, "\n".join(lines))
        return

    if text == "📋 Мои заказы":
        orders = await DB.passenger_orders(uid, limit=5)
        if not orders:
            await send(uid, "У вас нет заказов.")
            return
        for o in orders:
            hint = f"\n\nОтменить: отменить {o['id']}" if o["status"] in ("open","taken","pending") else ""
            await send(uid, fmt_order(o) + hint)
            await asyncio.sleep(0.3)
        return

    if text.startswith("отменить ") and text[9:].isdigit():
        oid = int(text[9:])
        if await DB.order_cancel_atomic(oid, uid, "passenger"):
            await send(uid, f"✅ Заказ #{oid} отменён.")
        else:
            await send(uid, "❌ Не удалось отменить заказ.")
        return

    # ── СОЗДАНИЕ ЗАКАЗА ──
    if text == "🚕 Создать заказ":
        recent = await DB.passenger_orders(uid, limit=1)
        if recent and recent[0]["status"] in ("open","taken","pending"):
            await send(uid, f"⚠️ У вас уже есть активный заказ #{recent[0]['id']}.\n"
                           f"Отменить: отменить {recent[0]['id']}")
            return
        clear_state(uid)
        set_state(uid, S.from_city)
        await send(uid, "📍 Шаг 1/7\nОткуда едем? Введите город:", keyboard=kb_cancel())
        return

    if state == S.from_city:
        update_data(uid, from_city=text)
        set_state(uid, S.to_city)
        await send(uid, f"✅ Откуда: {text}\n\n📍 Шаг 2/7\nКуда едем?")
        return

    if state == S.to_city:
        update_data(uid, to_city=text)
        set_state(uid, S.trip_date)
        await send(uid, f"✅ Куда: {text}\n\n📅 Шаг 3/7\nДата поездки (например: 15.06.2026):")
        return

    if state == S.trip_date:
        try:
            datetime.strptime(text, "%d.%m.%Y")
            update_data(uid, trip_date=text)
            set_state(uid, S.trip_time)
            await send(uid, f"✅ Дата: {text}\n\n🕐 Шаг 4/7\nВремя отправления (например: 10:30):")
        except ValueError:
            await send(uid, "❌ Неверный формат. Введите дату как: 15.06.2026")
        return

    if state == S.trip_time:
        try:
            datetime.strptime(text, "%H:%M")
            update_data(uid, trip_time=text)
            set_state(uid, S.passengers)
            await send(uid, f"✅ Время: {text}\n\n👥 Шаг 5/7\nСколько пассажиров?", keyboard=kb_passengers())
        except ValueError:
            await send(uid, "❌ Неверный формат. Введите время как: 10:30")
        return

    if state == S.passengers:
        if text.isdigit() and 1 <= int(text) <= 20:
            update_data(uid, passengers=int(text))
            set_state(uid, S.car_class)
            await send(uid, f"✅ Пассажиров: {text}\n\n🚗 Шаг 6/7\nКласс автомобиля:", keyboard=kb_car_class())
        else:
            await send(uid, "❌ Введите число от 1 до 20:")
        return

    if state == S.car_class:
        class_map = {v[0]: k for k, v in TARIFF_DATA.items()}
        if text in class_map:
            update_data(uid, car_class=class_map[text], car_class_label=text)
            set_state(uid, S.wishes)
            await send(uid, f"✅ Класс: {text}\n\n💬 Шаг 7/7\nПожелания? (или нажмите Нет)", keyboard=kb_yes_no())
        else:
            await send(uid, "❌ Выберите класс из списка:", keyboard=kb_car_class())
        return

    if state == S.wishes:
        wishes = None if text.lower() == "нет" else text
        update_data(uid, wishes=wishes)
        data = get_data(uid)
        clear_state(uid)
        await send(uid, "⏳ Рассчитываю маршрут...")
        asyncio.create_task(_finalize_order(uid, data))
        return

    # ── ВЗЯТЬ ЗАКАЗ ──
    if text.startswith("взять ") and text[6:].isdigit():
        await _take_order(uid, int(text[6:]))
        return

    # ── РЕГИСТРАЦИЯ ВОДИТЕЛЯ ──
    if text in ["👤 Я водитель", "Стать водителем", "Зарегистрироваться как водитель"]:
        if drv:
            await send(uid, "Вы уже зарегистрированы как водитель.", keyboard=kb_driver_main())
            return
        clear_state(uid)
        set_state(uid, S.first_name)
        await send(uid, "🚗 Регистрация водителя\n\nШаг 1/7\nВаше имя:", keyboard=kb_cancel())
        return

    if state == S.first_name:
        update_data(uid, first_name=text)
        set_state(uid, S.last_name)
        await send(uid, f"✅ Имя: {text}\n\nШаг 2/7\nФамилия:")
        return

    if state == S.last_name:
        update_data(uid, last_name=text)
        set_state(uid, S.phone)
        await send(uid, f"✅ Фамилия: {text}\n\nШаг 3/7\nНомер телефона:")
        return

    if state == S.phone:
        update_data(uid, phone=text)
        set_state(uid, S.car_model)
        await send(uid, f"✅ Телефон: {text}\n\nШаг 4/7\nМарка и модель авто (например: Toyota Camry):")
        return

    if state == S.car_model:
        update_data(uid, car_model=text)
        set_state(uid, S.car_year)
        await send(uid, f"✅ Авто: {text}\n\nШаг 5/7\nГод выпуска (например: 2019):")
        return

    if state == S.car_year:
        if text.isdigit() and 1990 <= int(text) <= now_dt().year:
            update_data(uid, car_year=int(text))
            set_state(uid, S.car_number)
            await send(uid, f"✅ Год: {text}\n\nШаг 6/7\nГос. номер (например: А777АА777):")
        else:
            await send(uid, f"❌ Введите корректный год (1990–{now_dt().year}):")
        return

    if state == S.car_number:
        update_data(uid, car_number=text.upper())
        set_state(uid, S.car_class)
        await send(uid, f"✅ Номер: {text}\n\nШаг 7/7\nКласс вашего авто:", keyboard=kb_car_class())
        return

    if state == S.car_class and get_data(uid).get("phone"):
        class_map = {v[0]: k for k, v in TARIFF_DATA.items()}
        if text in class_map:
            d = get_data(uid)
            d["car_class"]       = class_map[text]
            d["car_class_label"] = text
            d["name"]            = f"{d.get('first_name','')} {d.get('last_name','')}".strip()
            d["profile_link"]    = f"https://vk.com/id{uid}"
            d["registered_at"]   = now_iso()
            clear_state(uid)
            await DB.driver_save(uid, d)
            trial_exp  = (now_dt().date() + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d")
            await DB.sub_set(uid, trial_exp)
            trial_date = datetime.strptime(trial_exp, "%Y-%m-%d").strftime("%d.%m.%Y")
            await send(uid,
                f"🎉 Регистрация завершена!\n\n"
                f"👤 {d['name']}\n📞 {d.get('phone')}\n"
                f"🚘 {d.get('car_model')} ({d.get('car_year')})\n"
                f"🔢 {d.get('car_number')}\n🏷 {text}\n\n"
                f"🎁 Пробная подписка на {TRIAL_DAYS} дней!\nДо: {trial_date}\n\n"
                f"⏳ Ожидайте верификации администратором.",
                keyboard=kb_driver_main())
            for aid in ADMIN_VK_IDS:
                await send(aid,
                    f"🆕 Новый водитель (ВК)!\n\n"
                    f"👤 {d['name']}\n📞 {d.get('phone')}\n"
                    f"🚘 {d.get('car_model')} ({d.get('car_year')})\n"
                    f"🔢 {d.get('car_number')}\n🏷 {text}\n"
                    f"🔗 vk.com/id{uid}\n\n"
                    f"Верифицировать: верифицировать {uid}\n"
                    f"Удалить: удалить водителя {uid}")
        else:
            await send(uid, "❌ Выберите класс из списка:", keyboard=kb_car_class())
        return

    # ── ПРОФИЛЬ И МЕНЮ ВОДИТЕЛЯ ──
    if text == "👤 Мой профиль" and drv:
        exp, dl, active = await DB.sub_info(uid)
        sub_str = f"✅ До {exp} ({dl} дн.)" if active else "❌ Нет подписки"
        await send(uid,
            f"👤 {drv.get('first_name','')} {drv.get('last_name','')}\n"
            f"🚘 {drv.get('car_model')} ({drv.get('car_year','')})\n"
            f"🔢 {drv.get('car_number')}\n"
            f"🏷 {drv.get('car_class_label')}\n"
            f"📞 {drv.get('phone')}\n"
            f"📄 {'✅ Верифицирован' if drv.get('docs_verified') else '⏳ Ожидает верификации'}\n"
            f"💳 {sub_str}")
        return

    if text == "📦 Доступные заказы" and drv:
        if not drv.get("docs_verified"):
            await send(uid, "❌ Вы не верифицированы. Ожидайте проверки.")
            return
        _, _, active = await DB.sub_info(uid)
        if not active:
            await send(uid, "❌ Нет активной подписки.\n\nОформить: 💳 Абонемент")
            return
        orders = await DB.open_orders(limit=10)
        if not orders:
            await send(uid, "📦 Нет доступных заказов.")
            return
        for o in orders:
            if o.get("passenger_id") == uid: continue
            await send(uid, fmt_order(o) + f"\n\nВзять заказ: взять {o['id']}")
            await asyncio.sleep(0.3)
        return

    if text == "💳 Абонемент" and drv:
        exp, dl, active = await DB.sub_info(uid)
        status = f"✅ Активна до {exp} ({dl} дн.)" if active else "❌ Нет подписки"
        await send(uid, f"💳 Подписка: {status}\n\nВыберите тариф для оформления:", keyboard=kb_subs())
        return

    # ── ОФОРМЛЕНИЕ ПОДПИСКИ ──
    if drv and any(text == v["label"] for v in SUBS.values()):
        plan = next((k for k, v in SUBS.items() if v["label"] == text), None)
        if plan:
            sub = SUBS[plan]
            await DB.pending_set(uid, plan)
            await send(uid,
                f"💳 Заявка: {sub['label']}\n\n"
                f"{PAYMENT_DETAILS}\n\n"
                f"После оплаты администратор активирует подписку.")
            for aid in ADMIN_VK_IDS:
                await send(aid,
                    f"💳 Заявка на подписку (ВК)\n"
                    f"Водитель: vk.com/id{uid}\n"
                    f"План: {sub['label']}\n\n"
                    f"Активировать: активировать подписку {uid} {plan}\n"
                    f"Отклонить: отклонить подписку {uid}")
        return

    # ── КОМАНДЫ АДМИНА ──
    if is_adm and not state:
        if text == "👥 Водители":
            await _cmd_drivers(uid); return
        if text == "📋 Все заказы":
            await _cmd_orders(uid); return
        if text == "📊 Статистика":
            await _cmd_stats(uid); return
        if text == "💳 Выдать подписку":
            set_state(uid, S.sub_uid)
            await send(uid, "Введите VK ID водителя:", keyboard=kb_cancel()); return
        if text == "🚫 Забанить":
            set_state(uid, S.ban_uid)
            update_data(uid, unban=False)
            await send(uid, "Введите VK ID для бана:", keyboard=kb_cancel()); return
        if text == "✅ Разбанить":
            set_state(uid, S.ban_uid)
            update_data(uid, unban=True)
            await send(uid, "Введите VK ID для разбана:", keyboard=kb_cancel()); return
        if text == "🔙 Главное меню":
            clear_state(uid)
            await send(uid, "Меню:", keyboard=kb_admin()); return
        if text.startswith("верифицировать ") and text[15:].isdigit():
            tgt = int(text[15:])
            await DB.driver_verify(tgt, True)
            await send(uid, f"✅ Водитель vk.com/id{tgt} верифицирован.")
            await send(tgt, "✅ Ваш профиль верифицирован! Теперь вы можете принимать заказы.")
            return
        if text.startswith("удалить водителя ") and text[17:].isdigit():
            tgt = int(text[17:])
            async with _pg_pool.acquire() as c:
                await c.execute("DELETE FROM drivers WHERE user_id=$1", tgt)
            await send(uid, f"✅ Водитель vk.com/id{tgt} удалён.")
            await send(tgt, "❌ Ваш профиль водителя удалён.")
            return
        if text.startswith("активировать подписку "):
            parts = text.split()
            if len(parts) >= 4:
                tgt, plan_key = int(parts[2]), parts[3]
                if plan_key in SUBS:
                    sub = SUBS[plan_key]
                    exp, dl, active = await DB.sub_info(tgt)
                    base = date.fromisoformat(exp) if exp and active else now_dt().date()
                    new_exp = (base + timedelta(days=sub["days"])).isoformat()
                    await DB.sub_set(tgt, new_exp)
                    await DB.pending_del(tgt, uid, plan_key)
                    await send(uid, f"✅ Подписка активирована для vk.com/id{tgt} до {new_exp}")
                    await send(tgt, f"🎉 Абонемент активирован!\n{sub['label']}\nДо: {new_exp}")
            return
        if text.startswith("отклонить подписку ") and text[19:].isdigit():
            tgt = int(text[19:])
            plan_key = await DB.pending_get(tgt)
            if plan_key:
                await DB.pending_del(tgt, uid, plan_key)
                await send(uid, f"❌ Подписка отклонена для vk.com/id{tgt}")
                await send(tgt, "❌ Запрос на абонемент отклонён.")
            return
        if text.startswith("отменить заказ ") and text[15:].isdigit():
            oid = int(text[15:])
            await DB.order_upd(oid, status="cancelled")
            await send(uid, f"✅ Заказ #{oid} отменён.")
            o = await DB.order(oid)
            if o and o.get("passenger_id"):
                await send(o["passenger_id"], f"❌ Ваш заказ #{oid} отменён администратором.")
            return
        if text.startswith("завершить заказ ") and text[16:].isdigit():
            oid = int(text[16:])
            await DB.order_upd(oid, status="completed", completed_at=now_iso())
            await send(uid, f"✅ Заказ #{oid} завершён.")
            return

    # ── СОСТОЯНИЯ АДМИНА ──
    if state == S.sub_uid:
        if text.isdigit():
            update_data(uid, sub_tgt=int(text))
            set_state(uid, S.sub_days)
            await send(uid, f"Водитель: vk.com/id{text}\nСколько дней добавить?", keyboard=kb_cancel())
        else:
            await send(uid, "❌ Введите числовой VK ID:")
        return

    if state == S.sub_days:
        if text.isdigit():
            days = int(text); tgt = get_data(uid).get("sub_tgt")
            exp, dl, active = await DB.sub_info(tgt)
            base = date.fromisoformat(exp) if exp and active else now_dt().date()
            new_exp = (base + timedelta(days=days)).isoformat()
            await DB.sub_set(tgt, new_exp)
            clear_state(uid)
            await send(uid, f"✅ Подписка выдана vk.com/id{tgt} до {new_exp}", keyboard=kb_admin())
            await send(tgt, f"🎉 Вам выдана подписка на {days} дней!\nДо: {new_exp}")
        else:
            await send(uid, "❌ Введите количество дней:")
        return

    if state == S.ban_uid:
        if text.isdigit():
            tgt = int(text); unban = get_data(uid).get("unban", False)
            clear_state(uid)
            if unban:
                await DB.bl_remove(tgt); _BL_CACHE.discard(tgt)
                await send(uid, f"✅ Бан снят с vk.com/id{tgt}", keyboard=kb_admin())
            else:
                await DB.bl_add(tgt); _BL_CACHE.add(tgt)
                await send(uid, f"🚫 vk.com/id{tgt} заблокирован", keyboard=kb_admin())
        else:
            await send(uid, "❌ Введите числовой VK ID:")
        return

    # ── FALLBACK ──
    kb = kb_admin() if is_adm else (kb_driver_main() if drv else kb_main())
    await send(uid, "Выберите действие из меню:", keyboard=kb)


# ══════════════ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ══════════════
async def _take_order(uid, oid):
    drv = await DB.driver(uid)
    if not drv or not drv.get("docs_verified"):
        await send(uid, "❌ Вы не верифицированы.")
        return
    _, _, active = await DB.sub_info(uid)
    if not active:
        await send(uid, "❌ Нет активной подписки.\n\nОформить: 💳 Абонемент")
        return
    if not await DB.order_take_atomic(oid, uid):
        await send(uid, "⚠️ Заказ уже недоступен.")
        return
    order = await DB.order(oid)
    if not order:
        await send(uid, "✅ Заказ принят!")
        return
    drv_name = f"{drv.get('first_name','')} {drv.get('last_name','')}".strip()
    await send(uid,
        f"✅ Заказ #{oid} принят!\n\n"
        f"📍 {order.get('from_city')} → {order.get('to_city')}\n"
        f"📅 {order.get('trip_date')} в {order.get('trip_time')}\n"
        f"👥 {order.get('passengers')} чел.\n\n"
        f"Свяжитесь с пассажиром.")
    pid = order.get("passenger_id")
    if pid:
        await send(pid,
            f"🎉 Водитель найден!\n\n"
            f"👤 {drv_name}\n"
            f"🚘 {drv.get('car_model')} ({drv.get('car_year')})\n"
            f"🔢 {drv.get('car_number')}\n"
            f"📞 {drv.get('phone')}\n\n"
            f"🚫 Не переводите предоплату!\nОплата только водителю после поездки.")

async def _finalize_order(uid, data):
    try:
        loop = asyncio.get_running_loop()
        dist = await loop.run_in_executor(None, get_distance, data["from_city"], data["to_city"])
        dkm = price = None
        if dist:
            dkm   = round(dist * DIST_COEFF)
            price = calc_price(dkm, data["car_class"], data["from_city"], data["to_city"])
        oid   = await DB.order_create({**data, "passenger_id": uid,
                                       "distance_km": dkm, "price": price,
                                       "status": "open" if dkm else "pending"})
        order = await DB.order(oid)
        warn  = (
            f"✅ Заказ создан!\n\n{fmt_order(order)}\n\n"
            + ("" if dkm else "⚠️ Расстояние не рассчитано\n\n")
            + "⏳ Ожидайте — с вами свяжется водитель.\n\n"
            + "🚫 Не переводите предоплату!\nОплата только водителю после поездки."
        )
        await send(uid, warn)
        if dkm:
            await _notify_drivers(oid)
    except Exception as e:
        log.error(f"_finalize_order: {e}")
        await send(uid, "❌ Ошибка при создании заказа. Попробуйте ещё раз.")

async def _notify_drivers(oid):
    order = await DB.order(oid)
    if not order: return
    drivers = await DB.active_drivers()
    text = f"🔔 Новый заказ!\n\n{fmt_order(order)}\n\nВзять заказ: взять {oid}"
    for d in drivers:
        if d["user_id"] == order.get("passenger_id"): continue
        await send(d["user_id"], text)
        await asyncio.sleep(0.1)

async def _cmd_drivers(uid):
    drivers = await DB.all_drivers()
    if not drivers:
        await send(uid, "Водителей нет."); return
    for d in drivers[:10]:
        exp, dl, active = await DB.sub_info(d["user_id"])
        await send(uid,
            f"👤 {d.get('first_name','')} {d.get('last_name','')}\n"
            f"🚘 {d.get('car_model')} ({d.get('car_year','')})\n"
            f"📞 {d.get('phone')}\n"
            f"🔗 vk.com/id{d['user_id']}\n"
            f"📄 {'✅ Верифицирован' if d.get('docs_verified') else '⏳ Не верифицирован'}\n"
            f"💳 {'✅ До '+exp if active else '❌ Нет подписки'}\n\n"
            f"верифицировать {d['user_id']}\n"
            f"удалить водителя {d['user_id']}")
        await asyncio.sleep(0.3)

async def _cmd_orders(uid):
    orders = await DB.all_orders(limit=10)
    if not orders:
        await send(uid, "Заказов нет."); return
    for o in orders[:10]:
        await send(uid,
            fmt_order(o) +
            f"\n\nотменить заказ {o['id']}\n"
            f"завершить заказ {o['id']}")
        await asyncio.sleep(0.3)

async def _cmd_stats(uid):
    s = await DB.stats()
    await send(uid,
        f"📊 Статистика\n\n"
        f"📦 Всего заказов: {s['total']}\n"
        f"🟢 Открытых: {s['open']}\n"
        f"✅ Выполнено: {s['done']}\n"
        f"👥 Водителей: {s['drivers']}\n"
        f"✅ Верифицировано: {s['docs_ok']}\n"
        f"💳 Активных подписок: {s['subscribed']}")

# ══════════════ ФОНОВАЯ ЗАДАЧА ══════════════
async def _trial_expiry_notifier():
    while True:
        try:
            await asyncio.sleep(43200)  # каждые 12 часов
            tomorrow = (now_dt().date() + timedelta(days=1)).isoformat()
            async with _pg_pool.acquire() as c:
                rows = await c.fetch(
                    "SELECT user_id FROM subscriptions WHERE expires_date=$1", tomorrow)
            for r in rows:
                tuid = r["user_id"]
                drv  = await DB.driver(tuid)
                if drv:
                    await send(tuid,
                        "⚠️ Завтра заканчивается ваша подписка!\n\n"
                        "Оформить продление: 💳 Абонемент")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"_trial_expiry_notifier: {e}")
            await asyncio.sleep(60)

# ══════════════ ЗАПУСК ══════════════
async def main():
    log.info("=" * 50)
    log.info("  🚕 МЕЖГОРОД ТРАНСФЕР VK v1.0 (asyncpg)")
    log.info("=" * 50)
    await _init_pool()
    _BL_CACHE.update(await DB.bl_all())
    trial_task = asyncio.create_task(_trial_expiry_notifier())
    try:
        await bot.run_polling()
    finally:
        trial_task.cancel()
        try:
            await trial_task
        except asyncio.CancelledError:
            pass
        if _pg_pool:
            await _pg_pool.close()

if __name__ == "__main__":
    asyncio.run(main())
