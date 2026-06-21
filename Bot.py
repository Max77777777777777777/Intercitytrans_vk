import asyncio
import json
import logging
import os
import re
import random
from datetime import datetime, timedelta, timezone, date

import asyncpg
from geopy.geocoders import Yandex
from geopy.distance import geodesic as geo_dist

from vkbottle.bot import Bot, Message
from vkbottle import BaseStateGroup, BaseMiddleware, Keyboard, KeyboardButtonColor, Text, OpenLink

# === Настройки из env ===
VK_TOKEN = os.getenv("VK_TOKEN")
YANDEX_GEO_KEY = os.getenv("YANDEX_GEOCODER_KEY")
PG_DSN = os.getenv("PG_DSN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))
TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))
DIST_COEFF = 1.25

if not VK_TOKEN or not PG_DSN:
    raise RuntimeError("VK_TOKEN и PG_DSN должны быть установлены в переменных окружения")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=VK_TOKEN)
geolocator = Yandex(api_key=YANDEX_GEO_KEY) if YANDEX_GEO_KEY else None

_pg_pool = None
fsm_data = {}
fsm_data_ts = {}  # время последнего обновления для TTL-очистки
FSM_TTL_SECONDS = 3600  # 1 час — после этого зависшие сессии очищаются из RAM

# === Тарифы ===
NT_KW = ["лнр","днр","луганск","донецк","крым","симферополь","севастополь","херсон","запорожье","мариуполь","мелитополь"]

TARIFFS_RF = {
    "standard": {"label": "🚗 Стандарт", "price": 25},
    "comfort":  {"label": "🚙 Комфорт", "price": 34},
    "comfort_plus": {"label": "✨ Комфорт+", "price": 40},
    "minivan":  {"label": "🚐 Минивэн", "price": 45},
    "business": {"label": "💼 Бизнес", "price": 60},
}
TARIFFS_NT = {
    "standard": {"label": "🚗 Стандарт", "price": 40},
    "comfort":  {"label": "🚙 Комфорт", "price": 50},
    "comfort_plus": {"label": "✨ Комфорт+", "price": 58},
    "minivan":  {"label": "🚐 Минивэн", "price": 65},
    "business": {"label": "💼 Бизнес", "price": 80},
}

CLASS_LABELS = {
    "standard": "Стандарт", "comfort": "Комфорт",
    "comfort_plus": "Комфорт+", "minivan": "Минивэн", "business": "Бизнес"
}

def is_nt(city: str) -> bool:
    city_l = city.lower()
    # FIX: было kw in city_l — ловило «крым» внутри «Крымск» (Краснодарский край, не НТ).
    # \b работает корректно и для кириллицы, поэтому просто ищем слово целиком.
    return any(re.search(rf"\b{re.escape(kw)}\b", city_l) for kw in NT_KW)

def tariffs(cf: str, ct: str):
    return TARIFFS_NT if (is_nt(cf) or is_nt(ct)) else TARIFFS_RF

# === Подписки ===
SUBS = {
    "60":  {"days": 60,  "price": 650,  "label": "60 дней — 650 ₽"},
    "120": {"days": 120, "price": 1100, "label": "120 дней — 1 100 ₽"},
    "240": {"days": 240, "price": 2000, "label": "240 дней — 2 000 ₽"},
    "365": {"days": 365, "price": 3500, "label": "1 год — 3 500 ₽"},
}

# === FSM ===
# ВАЖНО: у OrderStates и DriverStates разные значения для car_class,
# иначе STATE_MAP не сможет различить в каком флоу находится пользователь.
class OrderStates(BaseStateGroup):
    from_city  = "ord_from_city"
    to_city    = "ord_to_city"
    trip_date  = "ord_trip_date"
    trip_time  = "ord_trip_time"
    passengers = "ord_passengers"
    car_class  = "ord_car_class"
    wishes     = "ord_wishes"

class DriverStates(BaseStateGroup):
    phone      = "drv_phone"
    car_model  = "drv_car_model"
    car_year   = "drv_car_year"
    car_number = "drv_car_number"
    car_class  = "drv_car_class"

class AdminEditStates(BaseStateGroup):
    waiting_input = "adm_edit_input"

adm_edit_fsm: dict = {}  # uid -> {target_uid, field}

# Маппинг строка→объект состояния для восстановления из БД
STATE_MAP = {
    "ord_from_city":  OrderStates.from_city,
    "ord_to_city":    OrderStates.to_city,
    "ord_trip_date":  OrderStates.trip_date,
    "ord_trip_time":  OrderStates.trip_time,
    "ord_passengers": OrderStates.passengers,
    "ord_car_class":  OrderStates.car_class,
    "ord_wishes":     OrderStates.wishes,
    "drv_phone":      DriverStates.phone,
    "drv_car_model":  DriverStates.car_model,
    "drv_car_year":   DriverStates.car_year,
    "drv_car_number": DriverStates.car_number,
    "drv_car_class":  DriverStates.car_class,
}

# === Клавиатуры ===
def kb_main(user_id=None):
    kb = Keyboard(inline=False)
    kb.add(Text("🚕 Создать заказ"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("🚗 Я водитель"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("📋 Мои заказы"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("📊 Тарифы"), color=KeyboardButtonColor.SECONDARY)
    if user_id in ADMIN_IDS:
        kb.row()
        kb.add(Text("🔧 Админ"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_driver_menu(has_sub: bool):
    kb = Keyboard(inline=False)
    kb.add(Text("📦 Доступные заказы"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("👤 Мой профиль"), color=KeyboardButtonColor.SECONDARY).row()
    kb.add(Text("💳 Абонемент"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("✅ Подписка активна" if has_sub else "❌ Нет подписки"), color=KeyboardButtonColor.SECONDARY).row()
    kb.add(Text("📈 Мои поездки"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("🔙 Главное меню"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_admin_menu():
    kb = Keyboard(inline=False)
    kb.add(Text("📋 Заказы"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("👥 Пользователи"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("✅ Верификация"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("💳 Подписки"), color=KeyboardButtonColor.SECONDARY).row()
    kb.add(Text("📊 Статистика"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("🔙 Главное меню"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_cancel():
    kb = Keyboard(inline=False)
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_skip():
    kb = Keyboard(inline=False)
    kb.add(Text("Пропустить"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_car_class_driver():
    kb = Keyboard(inline=False, one_time=False)
    kb.add(Text("Стандарт"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("Комфорт"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("Комфорт+"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("Минивэн"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("Бизнес"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

async def fsm_cleanup_task():
    """Фоновая задача: раз в 30 минут чистит зависшие FSM-сессии из RAM."""
    while True:
        await asyncio.sleep(1800)
        now = asyncio.get_event_loop().time()
        stale = [uid for uid, ts in list(fsm_data_ts.items()) if now - ts > FSM_TTL_SECONDS]
        for uid in stale:
            fsm_data.pop(uid, None)
            fsm_data_ts.pop(uid, None)
            try:
                await delete_fsm(uid)
            except Exception:
                pass
            # FIX: раньше чистили только RAM-кэш и БД, а bot.state_dispenser
            # (внутреннее хранилище vkbottle) оставался в старом состоянии —
            # следующее сообщение пользователя попадало в state-хендлер с уже
            # пустыми данными (например, регистрация водителя падала на NOT NULL).
            try:
                await bot.state_dispenser.delete(uid)
            except KeyError:
                pass
            except Exception:
                pass
        if stale:
            log.info(f"FSM cleanup: удалено {len(stale)} зависших сессий из RAM и БД")

async def auto_cancel_expired_orders():
    """Фоновая задача: каждые 5 минут отменяет просроченные открытые заказы."""
    while True:
        await asyncio.sleep(300)  # проверка каждые 5 минут
        try:
            async with _pg_pool.acquire() as conn:
                # Атомарно переводим все просроченные open-заказы в cancelled
                # и сразу возвращаем данные для уведомлений
                expired = await conn.fetch(
                    """
                    UPDATE orders SET status='cancelled', updated_at=NOW()
                    WHERE status = 'open'
                    AND (trip_date + trip_time)::timestamp <
                        (NOW() + INTERVAL '1 hour' * $1)::timestamp
                    RETURNING id, passenger_id
                    """,
                    TZ_OFFSET_HOURS
                )
            # Уведомляем пассажиров уже после закрытия соединения
            for o in expired:
                await safe_send(
                    o["passenger_id"],
                    f"😔 К сожалению, на ваш заказ #{o['id']} не нашлось водителя.\n"
                    f"Заказ автоматически отменён. Попробуйте оформить новый заказ "
                    f"или выберите другое время поездки."
                )
                log.info(f"Автоотмена: заказ #{o['id']} просрочен и отменён")
        except Exception as e:
            log.error(f"auto_cancel_expired_orders: {e}")

# === База данных ===
async def init_pg():
    global _pg_pool
    _pg_pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=10)
    async with _pg_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders(
                id SERIAL PRIMARY KEY,
                passenger_id BIGINT NOT NULL,
                from_city TEXT NOT NULL,
                to_city TEXT NOT NULL,
                trip_date DATE NOT NULL,
                trip_time TIME NOT NULL,
                passengers INT CHECK(passengers BETWEEN 1 AND 8),
                car_class TEXT NOT NULL,
                wishes TEXT,
                distance_km REAL,
                price INT,
                status TEXT DEFAULT 'open' CHECK(status IN ('open','taken','completed','cancelled')),
                driver_id BIGINT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS drivers(
                user_id BIGINT PRIMARY KEY,
                phone TEXT NOT NULL,
                car_model TEXT NOT NULL,
                car_year INT CHECK(car_year BETWEEN 2008 AND 2030),
                car_number TEXT NOT NULL,
                car_class TEXT NOT NULL,
                docs_verified BOOL DEFAULT FALSE,
                registered_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions(
                user_id BIGINT PRIMARY KEY,
                expires_date DATE NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_subscriptions(
                user_id BIGINT PRIMARY KEY,
                plan_key TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_log(
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                target_user_id BIGINT,
                plan_key TEXT,
                admin_id BIGINT,
                action TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ratings(
                id SERIAL PRIMARY KEY,
                order_id INT NOT NULL,
                driver_id BIGINT NOT NULL,
                passenger_id BIGINT NOT NULL,
                stars INT CHECK(stars BETWEEN 1 AND 5),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(order_id, passenger_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS blacklist(
                user_id BIGINT PRIMARY KEY,
                reason TEXT,
                banned_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fsm_store(
                user_id BIGINT PRIMARY KEY,
                state TEXT NOT NULL,
                data JSONB DEFAULT '{}'::jsonb
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_passenger ON orders(passenger_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_driver ON orders(driver_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        log.info("✅ База данных инициализирована")

# --- FSM persistence ---
async def save_fsm(user_id: int, state, data: dict):
    # Принимаем и enum-объект и строку
    state_str = state.value if hasattr(state, 'value') else str(state)
    fsm_data_ts[user_id] = asyncio.get_event_loop().time()  # обновляем TTL
    async with _pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO fsm_store (user_id, state, data) VALUES ($1,$2,$3::jsonb) "
            "ON CONFLICT (user_id) DO UPDATE SET state=$2, data=$3::jsonb",
            user_id, state_str, json.dumps(data, ensure_ascii=False)
        )

async def load_fsm(user_id: int):
    async with _pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT state, data FROM fsm_store WHERE user_id=$1", user_id)
        if row:
            # asyncpg возвращает JSONB уже как dict
            return row['state'], row['data'] or {}
        return None, {}

async def delete_fsm(user_id: int):
    async with _pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM fsm_store WHERE user_id=$1", user_id)

class FSMRestoreMiddleware(BaseMiddleware[Message]):
    """Восстанавливает состояние FSM из БД ДО роутинга — иначе state-хендлеры
    не сработают, если RAM-кэш (state_dispenser) был очищен после перезапуска."""
    async def pre(self):
        uid = self.event.from_id
        # Системные команды сброса — не восстанавливаем FSM,
        # чтобы /start мог корректно очистить состояние в своём хендлере
        RESET_COMMANDS = {"/start", "Начать", "🔙 Главное меню"}
        if self.event.text in RESET_COMMANDS:
            return
        try:
            current_state = await bot.state_dispenser.get(uid)
        except KeyError:
            current_state = None
        if current_state is None:
            saved_state_str, saved_data = await load_fsm(uid)
            if saved_state_str is not None:
                state_obj = STATE_MAP.get(saved_state_str)
                if state_obj:
                    await bot.state_dispenser.set(uid, state_obj)
                    fsm_data[uid] = saved_data
                    fsm_data_ts[uid] = asyncio.get_event_loop().time()
                    log.info(f"FSM восстановлено через Middleware для {uid}: {saved_state_str}")
                else:
                    log.warning(f"Неизвестное состояние в БД для {uid}: {saved_state_str!r}, очищаем")
                    await delete_fsm(uid)

# === Утилиты ===
def esc(text):
    return str(text) if text else "—"

def vk_link(user_id, label=None):
    """Кликабельное упоминание профиля ВК в формате [id123|имя]"""
    label = label or f"ID {user_id}"
    return f"[id{user_id}|{label}]"

def is_valid_city(city):
    return bool(city) and len(city.strip()) >= 2

def is_valid_date(text):
    try:
        d = datetime.strptime(text, "%d.%m.%Y")
        today = datetime.now(TZ).date()
        return today <= d.date() <= today + timedelta(days=365)
    except:
        return False

def is_valid_time(text):
    if not re.match(r"^\d{1,2}:\d{2}$", text):
        return False
    try:
        h, m = map(int, text.split(":"))
        return 0 <= h <= 23 and 0 <= m <= 59
    except:
        return False

def is_valid_phone(text):
    cleaned = text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    return bool(re.match(r"^\+?[78][0-9]{10}$", cleaned))

def is_valid_car_number(text):
    # FIX: было жёстко под формат РФ (1 буква-3 цифры-2 буквы-2/3 цифры),
    # отсеивало нормальные номера других стран (Армения, Киргизия и т.д.).
    # Теперь просто буквы (кириллица или латиница) и цифры, разумная длина.
    # Точный формат при необходимости поправит админ вручную при верификации.
    cleaned = (text or "").strip().upper().replace(" ", "")
    return bool(re.match(r"^[A-ZА-Я0-9]{4,12}$", cleaned))

# Синхронная основа (блокирующая) — вызывается только через asyncio.to_thread
def _geocode_sync(city):
    if not geolocator or not city:
        return None
    try:
        loc = geolocator.geocode(city, timeout=5)
        if loc:
            return loc.latitude, loc.longitude
    except Exception as ex:
        log.error(f"Geocode error for '{city}': {ex}")
    return None

async def geocode_async(city):
    """Асинхронная обёртка — не блокирует event loop."""
    return await asyncio.to_thread(_geocode_sync, city)

async def calculate_distance_async(city_from, city_to):
    """Два запроса параллельно через asyncio.gather."""
    coords_from, coords_to = await asyncio.gather(
        geocode_async(city_from),
        geocode_async(city_to)
    )
    if coords_from and coords_to:
        distance = geo_dist(coords_from, coords_to).kilometers
        return round(distance * DIST_COEFF)
    return None

def calculate_price(distance_km, car_class, from_city="", to_city=""):
    if distance_km is None or (car_class not in TARIFFS_RF and car_class not in TARIFFS_NT):
        return None
    t = tariffs(from_city, to_city)
    price_per_km = t.get(car_class, t.get("standard", {"price": 30}))["price"]
    return round(distance_km * price_per_km)

async def check_blacklist(user_id):
    async with _pg_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT 1 FROM blacklist WHERE user_id=$1", user_id)
    return r is not None

def can_take_order(driver_class, order_class):
    level = {"standard": 0, "comfort": 1, "comfort_plus": 2, "minivan": 3, "business": 4}
    d_lvl = level.get(driver_class, 0)
    o_lvl = level.get(order_class, 0)
    if order_class == "minivan" and driver_class != "minivan":
        return False, "Заказы минивэна — только для минивэнов"
    if order_class == "business" and driver_class != "business":
        return False, "Заказы бизнес-класса — только для бизнес-авто"
    if d_lvl < o_lvl:
        return False, f"Ваш класс ({driver_class}) ниже требуемого ({order_class})"
    return True, ""

def fmt_order(o):
    status_map = {
        'open': '🟢 Открыт',
        'taken': '🟡 Принят',
        'completed': '✅ Завершён',
        'cancelled': '❌ Отменён'
    }
    distance_str = f"{int(o.get('distance_km'))} км" if o.get('distance_km') is not None else "—"
    price_str = f"{o.get('price')} ₽" if o.get('price') is not None else "—"
    lines = [
        f"🚕 Заказ #{o['id']}",
        f"📍 {esc(o['from_city'])} → {esc(o['to_city'])}",
        f"📅 {o['trip_date'].strftime('%d.%m.%Y') if hasattr(o['trip_date'], 'strftime') else o['trip_date']} "
        f"🕐 {o['trip_time'].strftime('%H:%M') if hasattr(o['trip_time'], 'strftime') else o['trip_time']}",
        f"👥 Пассажиров: {o['passengers']}",
        f"🚘 Класс: {CLASS_LABELS.get(o['car_class'], esc(o['car_class']))}",
        f"📏 Расстояние: {distance_str}",
        f"💰 Цена: {price_str}",
        f"Статус: {status_map.get(o['status'], o['status'])}",
    ]
    if o.get('wishes'):
        lines.append(f"💬 Пожелания: {esc(o['wishes'])}")
    return "\n".join(lines)

async def safe_send(user_id, text, keyboard=None):
    try:
        await bot.api.messages.send(
            user_id=user_id,
            message=text,
            keyboard=keyboard,
            random_id=random.randint(1, 2**31 - 1)
        )
    except Exception as e:
        log.error(f"Ошибка отправки пользователю {user_id}: {e}")

def parse_payload(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}

async def has_active_sub(user_id):
    async with _pg_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT expires_date FROM subscriptions WHERE user_id=$1", user_id)
        if not r or not r['expires_date']:
            return False
        try:
            exp = r['expires_date'] if not isinstance(r['expires_date'], str) \
                else datetime.strptime(r['expires_date'], "%Y-%m-%d").date()
            return exp >= datetime.now(TZ).date()
        except:
            return False

async def is_driver_registered(user_id):
    async with _pg_pool.acquire() as conn:
        drv = await conn.fetchrow("SELECT 1 FROM drivers WHERE user_id=$1", user_id)
        return drv is not None

async def is_driver_verified(user_id):
    async with _pg_pool.acquire() as conn:
        drv = await conn.fetchrow("SELECT docs_verified FROM drivers WHERE user_id=$1", user_id)
        return drv and drv['docs_verified']

async def is_driver_busy(driver_id):
    async with _pg_pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT COUNT(*) as cnt FROM orders WHERE driver_id=$1 AND status='taken'",
            driver_id
        )
        return r['cnt'] > 0

async def get_order(order_id):
    async with _pg_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)

async def update_order_status(order_id, status, driver_id=None, clear_driver=False):
    async with _pg_pool.acquire() as conn:
        if driver_id is not None:
            await conn.execute(
                "UPDATE orders SET status=$1, driver_id=$2, updated_at=NOW() WHERE id=$3",
                status, driver_id, order_id
            )
        elif clear_driver:
            # Явно сбрасываем driver_id (водитель отказался)
            await conn.execute(
                "UPDATE orders SET status=$1, driver_id=NULL, updated_at=NOW() WHERE id=$2",
                status, order_id
            )
        else:
            # Не трогаем driver_id — только меняем статус
            await conn.execute(
                "UPDATE orders SET status=$1, updated_at=NOW() WHERE id=$2",
                status, order_id
            )

async def try_take_order_atomic(order_id, driver_id, driver_class):
    async with _pg_pool.acquire() as conn:
        async with conn.transaction():
            order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1 FOR UPDATE", order_id)
            if not order:
                return False, "Заказ не найден", None
            if order['status'] != 'open':
                return False, "Заказ уже принят или отменён", None
            if order['passenger_id'] == driver_id:
                return False, "Вы не можете взять свой заказ", None
            allowed, reason = can_take_order(driver_class, order['car_class'])
            if not allowed:
                return False, reason, None
            await conn.execute(
                "UPDATE orders SET status='taken', driver_id=$1, updated_at=NOW() WHERE id=$2",
                driver_id, order_id
            )
            updated = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
            return True, "", updated

async def has_rating(order_id, passenger_id):
    async with _pg_pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT 1 FROM ratings WHERE order_id=$1 AND passenger_id=$2",
            order_id, passenger_id
        )
    return r is not None

async def add_rating(order_id, driver_id, passenger_id, stars):
    async with _pg_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ratings(order_id, driver_id, passenger_id, stars, created_at)
               VALUES($1,$2,$3,$4,$5)
               ON CONFLICT (order_id, passenger_id)
               DO UPDATE SET stars=$4, created_at=$5""",
            order_id, driver_id, passenger_id, stars, datetime.now(TZ)
        )

async def avg_rating(driver_id):
    async with _pg_pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT COALESCE(AVG(stars)::float, 0) as avg, COUNT(*)::int as cnt FROM ratings WHERE driver_id=$1",
            driver_id
        )
    if r and r['cnt'] > 0:
        return round(r['avg'], 1), r['cnt']
    return 0.0, 0

async def notify_drivers_about_order(order_id, exclude_drivers=None):
    if exclude_drivers is None:
        exclude_drivers = []
    try:
        async with _pg_pool.acquire() as conn:
            drivers = await conn.fetch("""
                SELECT d.user_id, d.car_class FROM drivers d
                JOIN subscriptions s ON d.user_id = s.user_id
                WHERE d.docs_verified = TRUE AND s.expires_date >= NOW()::date
            """)
            order = await get_order(order_id)
            if not order or order['status'] != 'open':
                return
        text_notify = f"🔔 Новый заказ #{order_id}:\n\n{fmt_order(order)}"
        kb = Keyboard(inline=True)
        kb.add(
            Text("✅ Взять заказ", payload={"cmd": "take_order", "order_id": order_id}),
            color=KeyboardButtonColor.POSITIVE
        ).row()
        kb.add(
            Text("➡️ Пропустить", payload={"cmd": "skip_order", "order_id": order_id}),
            color=KeyboardButtonColor.SECONDARY
        )
        kjson = kb.get_json()
        sent_count = 0
        for d in drivers:
            if d['user_id'] in exclude_drivers:
                continue
            if await is_driver_busy(d['user_id']):
                continue
            allowed, _ = can_take_order(d['car_class'], order['car_class'])
            if not allowed:
                continue
            await safe_send(d['user_id'], text_notify, keyboard=kjson)
            sent_count += 1
            await asyncio.sleep(0.1)
        log.info(f"Уведомление о заказе #{order_id} отправлено {sent_count} водителям")
    except Exception as e:
        log.error(f"Ошибка при рассылке уведомлений о заказе #{order_id}: {e}", exc_info=True)

# ================= СТАРТ / ГЛАВНОЕ МЕНЮ =================
@bot.on.message(text=["/start", "Начать", "🔙 Главное меню"])
async def start_handler(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    uid = message.from_id
    try:
        await bot.state_dispenser.delete(uid)
    except KeyError:
        pass
    fsm_data.pop(uid, None)
    await delete_fsm(uid)
    await message.answer(
        "🚕 Добро пожаловать в сервис междугородних поездок!\n\n"
        "Я помогу найти попутную машину или заказать поездку между городами.\n\n"
        "Выберите действие в меню:",
        keyboard=kb_main(uid)
    )

# ================= СОЗДАНИЕ ЗАКАЗА (FSM) =================
@bot.on.message(text="🚕 Создать заказ")
async def start_order(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    uid = message.from_id
    await bot.state_dispenser.set(uid, OrderStates.from_city)
    fsm_data[uid] = {}
    await save_fsm(uid, OrderStates.from_city, {})
    await message.answer("📍 Введите город отправления:", keyboard=kb_cancel())

@bot.on.message(state=OrderStates.from_city)
async def order_from_city(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(uid))
        return
    if not is_valid_city(message.text):
        await message.answer("❌ Некорректное название города. Введите ещё раз:")
        return
    city = message.text.strip()
    if geolocator and not await geocode_async(city):
        await message.answer("❌ Город не найден на карте. Проверьте название и введите ещё раз:")
        return
    fsm_data.setdefault(uid, {})["from_city"] = city
    await bot.state_dispenser.set(uid, OrderStates.to_city)
    await save_fsm(uid, OrderStates.to_city, fsm_data[uid])
    await message.answer("📍 Введите город назначения:")

@bot.on.message(state=OrderStates.to_city)
async def order_to_city(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(uid))
        return
    if not is_valid_city(message.text):
        await message.answer("❌ Некорректное название города. Введите ещё раз:")
        return
    data = fsm_data.get(uid, {})
    from_city = data.get("from_city", "")
    city = message.text.strip()
    if city.lower() == from_city.lower():
        await message.answer("❌ Город отправления и назначения не должны совпадать. Введите другой город:")
        return
    if geolocator and not await geocode_async(city):
        await message.answer("❌ Город не найден на карте. Проверьте название и введите ещё раз:")
        return
    data["to_city"] = city
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, OrderStates.trip_date)
    await save_fsm(uid, OrderStates.trip_date, data)
    await message.answer("📅 Введите дату поездки в формате ДД.ММ.ГГГГ:")

@bot.on.message(state=OrderStates.trip_date)
async def order_trip_date(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(uid))
        return
    if not is_valid_date(message.text):
        await message.answer("❌ Некорректная дата. Введите дату в формате ДД.ММ.ГГГГ (не ранее сегодня и не позже года):")
        return
    data = fsm_data.get(uid, {})
    data["trip_date"] = message.text.strip()
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, OrderStates.trip_time)
    await save_fsm(uid, OrderStates.trip_time, data)
    await message.answer("🕐 Введите время поездки в формате ЧЧ:ММ:")

@bot.on.message(state=OrderStates.trip_time)
async def order_trip_time(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(uid))
        return
    if not is_valid_time(message.text):
        await message.answer("❌ Некорректное время. Введите время в формате ЧЧ:ММ (например, 14:30):")
        return
    data = fsm_data.get(uid, {})
    trip_date_str = data.get("trip_date")
    try:
        trip_datetime = datetime.strptime(f"{trip_date_str} {message.text}", "%d.%m.%Y %H:%M")
        trip_datetime = trip_datetime.replace(tzinfo=TZ)
        if trip_datetime < datetime.now(TZ):
            await message.answer("❌ Время поездки не может быть в прошлом. Введите корректное время:")
            return
    except Exception:
        await message.answer("❌ Ошибка при обработке даты и времени. Введите корректное время:")
        return
    data["trip_time"] = message.text.strip()
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, OrderStates.passengers)
    await save_fsm(uid, OrderStates.passengers, data)
    await message.answer("👥 Введите количество пассажиров (1-8):")

@bot.on.message(state=OrderStates.passengers)
async def order_passengers(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(uid))
        return
    try:
        passengers = int(message.text)
        if passengers < 1 or passengers > 8:
            raise ValueError
    except:
        await message.answer("❌ Введите число от 1 до 8:")
        return
    data = fsm_data.get(uid, {})
    data["passengers"] = passengers
    fsm_data[uid] = data
    kb = Keyboard(inline=False)
    if passengers >= 5:
        # Для 5+ пассажиров — только минивэн
        kb.add(Text("Минивэн"), color=KeyboardButtonColor.PRIMARY).row()
        hint = "\n\n⚠️ Для 5 и более пассажиров доступен только Минивэн!"
    else:
        kb.add(Text("Стандарт"), color=KeyboardButtonColor.PRIMARY)
        kb.add(Text("Комфорт"), color=KeyboardButtonColor.PRIMARY).row()
        kb.add(Text("Комфорт+"), color=KeyboardButtonColor.PRIMARY)
        kb.add(Text("Минивэн"), color=KeyboardButtonColor.PRIMARY).row()
        kb.add(Text("Бизнес"), color=KeyboardButtonColor.PRIMARY).row()
        hint = ""
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    await bot.state_dispenser.set(uid, OrderStates.car_class)
    await save_fsm(uid, OrderStates.car_class, data)
    await message.answer(f"🚘 Выберите класс автомобиля:{hint}", keyboard=kb.get_json())

@bot.on.message(state=OrderStates.car_class)
async def order_car_class(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(uid))
        return
    class_map = {
        "стандарт": "standard", "комфорт": "comfort",
        "комфорт+": "comfort_plus", "минивэн": "minivan", "бизнес": "business"
    }
    car_class = class_map.get(message.text.lower())
    if not car_class:
        await message.answer("❌ Выберите класс из списка на клавиатуре:")
        return
    data = fsm_data.get(uid, {})
    # FIX: серверная проверка, а не только урезанная клавиатура — иначе можно
    # вписать класс вручную или нажать кнопку из старого сообщения в истории чата
    if data.get("passengers", 0) >= 5 and car_class != "minivan":
        await message.answer("❌ Для 5 и более пассажиров доступен только Минивэн. Выберите класс из списка на клавиатуре:")
        return
    data["car_class"] = car_class
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, OrderStates.wishes)
    await save_fsm(uid, OrderStates.wishes, data)
    await message.answer("💬 Введите дополнительные пожелания (или нажмите 'Пропустить'):", keyboard=kb_skip())

@bot.on.message(state=OrderStates.wishes)
async def order_wishes(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(uid))
        return

    # FIX: используем get(), а не pop() — удаляем только после успешного INSERT
    data = fsm_data.get(uid, {})

    # Fallback: если ключевые поля отсутствуют (сессия устарела в RAM и данные
    # не восстановились из БД полностью), просим начать заново
    required_keys = ('from_city', 'to_city', 'trip_date', 'trip_time', 'passengers', 'car_class')
    if any(k not in data for k in required_keys):
        await bot.state_dispenser.delete(uid)
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer(
            "⚠️ Сессия устарела или данные потеряны. Пожалуйста, создайте заказ заново.",
            keyboard=kb_main(uid)
        )
        return

    wishes = message.text if message.text != "Пропустить" else ""

    distance = await calculate_distance_async(data['from_city'], data['to_city'])
    price = calculate_price(distance, data['car_class'], data['from_city'], data['to_city'])

    # FIX: asyncpg требует объекты date/time, а не строки
    trip_date = datetime.strptime(data['trip_date'], "%d.%m.%Y").date()
    trip_time = datetime.strptime(data['trip_time'], "%H:%M").time()

    async with _pg_pool.acquire() as conn:
        order = await conn.fetchrow(
            """INSERT INTO orders(
                passenger_id, from_city, to_city, trip_date, trip_time,
                passengers, car_class, wishes, distance_km, price
            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            RETURNING id""",
            uid, data['from_city'], data['to_city'],
            trip_date, trip_time, data['passengers'],
            data['car_class'], wishes, distance, price
        )
        order_id = order['id']

    # Очищаем FSM только после успешного INSERT
    fsm_data.pop(uid, None)
    try:
        await bot.state_dispenser.delete(uid)
    except KeyError:
        pass
    await delete_fsm(uid)

    distance_str = f"{int(distance)} км" if distance is not None else "—"
    price_str = f"{price} ₽" if price is not None else "—"
    confirm_text = (
        f"✅ Заказ #{order_id} создан!\n\n"
        f"📍 {data['from_city']} → {data['to_city']}\n"
        f"📅 {data['trip_date']} в {data['trip_time']}\n"
        f"👥 Пассажиров: {data['passengers']}\n"
        f"🚘 Класс: {CLASS_LABELS.get(data['car_class'], data['car_class'])}\n"
        f"📏 Расстояние: {distance_str}\n"
        f"💰 Цена: {price_str}\n"
    )
    if wishes:
        confirm_text += f"💬 Пожелания: {wishes}\n"
    confirm_text += "\n🔍 Ищем подходящих водителей..."

    await message.answer(confirm_text, keyboard=kb_main(uid))
    asyncio.create_task(notify_drivers_about_order(order_id))

# ================= РЕГИСТРАЦИЯ ВОДИТЕЛЯ (FSM) =================
@bot.on.message(text="🚗 Я водитель")
async def driver_menu_handler(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    uid = message.from_id
    try:
        await bot.state_dispenser.delete(uid)
    except KeyError:
        pass

    is_registered = await is_driver_registered(uid)
    is_verified = await is_driver_verified(uid)
    has_sub = await has_active_sub(uid)

    if not is_registered:
        await bot.state_dispenser.set(uid, DriverStates.phone)
        fsm_data[uid] = {}
        await save_fsm(uid, DriverStates.phone, {})
        await message.answer(
            "📱 Для регистрации водителем введите ваш номер телефона в формате +79991234567:",
            keyboard=kb_cancel()
        )
        return

    await delete_fsm(uid)
    fsm_data.pop(uid, None)

    if not is_verified:
        await message.answer(
            "⏳ Ваш профиль водителя ожидает верификации администратором.",
            keyboard=kb_driver_menu(has_sub)
        )
        return

    if not has_sub:
        await message.answer(
            "⚠️ У вас нет активной подписки. Приобретите подписку в разделе '💳 Абонемент'.",
            keyboard=kb_driver_menu(has_sub)
        )
        return

    await message.answer("Выберите действие:", keyboard=kb_driver_menu(has_sub))

@bot.on.message(state=DriverStates.phone)
async def driver_phone(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Регистрация отменена.", keyboard=kb_main(uid))
        return
    if not is_valid_phone(message.text):
        await message.answer("❌ Некорректный номер. Введите в формате +79991234567:")
        return
    fsm_data.setdefault(uid, {})["phone"] = message.text.strip()
    await bot.state_dispenser.set(uid, DriverStates.car_model)
    await save_fsm(uid, DriverStates.car_model, fsm_data[uid])
    await message.answer("🚗 Введите марку и модель автомобиля:")

@bot.on.message(state=DriverStates.car_model)
async def driver_car_model(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Регистрация отменена.", keyboard=kb_main(uid))
        return
    if len(message.text.strip()) < 2:
        await message.answer("❌ Слишком короткое название. Введите марку и модель:")
        return
    data = fsm_data.get(uid, {})
    data['car_model'] = message.text.strip()
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, DriverStates.car_year)
    await save_fsm(uid, DriverStates.car_year, data)
    await message.answer("📅 Введите год выпуска автомобиля:")

@bot.on.message(state=DriverStates.car_year)
async def driver_car_year(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Регистрация отменена.", keyboard=kb_main(uid))
        return
    try:
        year = int(message.text)
        if year < 2008 or year > 2030:
            raise ValueError
    except:
        await message.answer("❌ Введите год от 2008 до 2030:")
        return
    data = fsm_data.get(uid, {})
    data['car_year'] = year
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, DriverStates.car_number)
    await save_fsm(uid, DriverStates.car_number, data)
    await message.answer("🔢 Введите госномер автомобиля (буквы и цифры, например А123БВ178):")

@bot.on.message(state=DriverStates.car_number)
async def driver_car_number(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Регистрация отменена.", keyboard=kb_main(uid))
        return
    if not is_valid_car_number(message.text):
        await message.answer("❌ Некорректный номер. Введите буквы и цифры, 4–12 символов:")
        return
    data = fsm_data.get(uid, {})
    data['car_number'] = message.text.strip().upper().replace(" ", "")
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, DriverStates.car_class)
    await save_fsm(uid, DriverStates.car_class, data)
    # Сброс клавиатуры — воркэраунд бага ВК с пустым отображением кнопок
    await message.answer("⏳", keyboard='{"buttons":[],"one_time":true}')
    await message.answer("🚘 Выберите класс вашего автомобиля:", keyboard=kb_car_class_driver())

@bot.on.message(state=DriverStates.car_class)
async def driver_car_class(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        try:
            await bot.state_dispenser.delete(uid)
        except KeyError:
            pass
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await message.answer("Регистрация отменена.", keyboard=kb_main(uid))
        return
    class_map = {
        "стандарт": "standard", "комфорт": "comfort",
        "комфорт+": "comfort_plus", "минивэн": "minivan", "бизнес": "business"
    }
    car_class = class_map.get(message.text.lower())
    if not car_class:
        await message.answer("❌ Выберите класс из списка на клавиатуре:")
        return

    # FIX: get() вместо pop() — удаляем после успешного INSERT
    data = fsm_data.get(uid, {})

    async with _pg_pool.acquire() as conn:
        # Проверяем — новая регистрация или повторная
        existing = await conn.fetchrow("SELECT user_id FROM drivers WHERE user_id=$1", uid)
        await conn.execute(
            """INSERT INTO drivers(user_id, phone, car_model, car_year, car_number, car_class)
               VALUES($1,$2,$3,$4,$5,$6)
               ON CONFLICT (user_id) DO UPDATE SET
               phone=$2, car_model=$3, car_year=$4, car_number=$5, car_class=$6, docs_verified=FALSE""",
            uid, data.get('phone'), data.get('car_model'),
            data.get('car_year'), data.get('car_number'), car_class
        )
        # Пробная подписка на 50 дней — только при первой регистрации
        trial_exp = None
        if not existing:
            trial_exp = date.today() + timedelta(days=50)
            await conn.execute(
                "INSERT INTO subscriptions(user_id, expires_date) VALUES($1,$2) ON CONFLICT DO NOTHING",
                uid, trial_exp
            )
            log.info(f"Пробная подписка выдана водителю {uid} до {trial_exp}")

    fsm_data.pop(uid, None)
    try:
        await bot.state_dispenser.delete(uid)
    except KeyError:
        pass
    await delete_fsm(uid)

    for admin_id in ADMIN_IDS:
        kb = Keyboard(inline=True)
        kb.add(
            Text("✅ Верифицировать", payload={"cmd": "verify_driver", "user_id": uid}),
            color=KeyboardButtonColor.POSITIVE
        ).row()
        kb.add(
            Text("❌ Отклонить", payload={"cmd": "reject_driver", "user_id": uid}),
            color=KeyboardButtonColor.NEGATIVE
        )
        admin_text = (
            f"🔔 Новый водитель на проверку:\n"
            f"{vk_link(uid)}\n"
            f"Телефон: {data.get('phone')}\n"
            f"Авто: {data.get('car_model')} ({data.get('car_year')})\n"
            f"Номер: {data.get('car_number')}\n"
            f"Класс: {CLASS_LABELS.get(car_class, car_class)}"
        )
        await safe_send(admin_id, admin_text, keyboard=kb.get_json())

    trial_txt = (
        f"\n\n🎁 Вам начислена бесплатная пробная подписка на 50 дней!\n"
        f"Активна до: {trial_exp.strftime('%d.%m.%Y')}\n"
        f"Вы сможете принимать заказы сразу после верификации."
        if trial_exp else ""
    )
    await message.answer(
        f"✅ Регистрация завершена! Ваша заявка отправлена на проверку администратору.{trial_txt}",
        keyboard=kb_driver_menu(False)
    )

# ================= ДОСТУПНЫЕ ЗАКАЗЫ =================
@bot.on.message(text="📦 Доступные заказы")
async def available_orders(message: Message):
    uid = message.from_id
    if await check_blacklist(uid):
        return await message.answer("⛔ Вы заблокированы.")
    if not await is_driver_registered(uid):
        return await message.answer("❌ Сначала зарегистрируйтесь как водитель.")
    if not await has_active_sub(uid):
        return await message.answer("🔒 Нет абонемента.")
    if not await is_driver_verified(uid):
        return await message.answer("⏳ Профиль ещё не верифицирован.")
    async with _pg_pool.acquire() as conn:
        driver = await conn.fetchrow("SELECT car_class FROM drivers WHERE user_id=$1", uid)
        if not driver:
            return await message.answer("❌ Профиль водителя не найден.")
        open_orders = await conn.fetch(
            "SELECT * FROM orders WHERE status='open' AND passenger_id != $1 ORDER BY created_at DESC", uid
        )
    if not open_orders:
        return await message.answer("📭 Нет доступных заказов.")

    PAGE_SIZE = 5
    can_count = 0
    shown = 0
    for o in open_orders:
        allowed, _ = can_take_order(driver['car_class'], o['car_class'])
        if allowed:
            can_count += 1
        if shown >= PAGE_SIZE:
            continue
        shown += 1
        kb = Keyboard(inline=True)
        if allowed:
            kb.add(
                Text("✅ Взять", payload={"cmd": "take_order", "order_id": o['id']}),
                color=KeyboardButtonColor.POSITIVE
            )
        else:
            kb.add(
                Text("🔒 Недоступен", payload={"cmd": "skip_order", "order_id": o['id']}),
                color=KeyboardButtonColor.SECONDARY
            )
        await message.answer(fmt_order(o), keyboard=kb.get_json())
        await asyncio.sleep(0.05)  # защита от rate limit

    total = len(open_orders)
    summary = f"📦 Открыто: {total} | ✅ Доступно: {can_count}"
    if total > PAGE_SIZE:
        summary += f"\n(показано {PAGE_SIZE} из {total}, остальные появятся по мере выполнения)"
    await message.answer(summary, keyboard=kb_driver_menu(True))

# === Кнопки статуса подписки ===
@bot.on.message(text=["✅ Подписка активна", "❌ Нет подписки"])
async def sub_status_handler(message: Message):
    await subscription_handler(message)

# === Мои заказы ===
@bot.on.message(text="📋 Мои заказы")
async def passenger_orders_list(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    async with _pg_pool.acquire() as conn:
        orders = await conn.fetch(
            "SELECT * FROM orders WHERE passenger_id=$1 ORDER BY created_at DESC LIMIT 10",
            message.from_id
        )
    if not orders:
        await message.answer("У вас пока нет заказов.", keyboard=kb_main(message.from_id))
        return
    for o in orders:
        text = fmt_order(o)
        kb = None
        if o['status'] in ['open', 'taken']:
            kb = Keyboard(inline=True)
            kb.add(
                Text("❌ Отменить заказ", payload={"cmd": "cancel_order", "order_id": o['id']}),
                color=KeyboardButtonColor.NEGATIVE
            )
        elif o['status'] == 'completed' and not await has_rating(o['id'], message.from_id):
            kb = Keyboard(inline=True)
            for i in range(1, 6):
                kb.add(Text(f"{i}⭐", payload={"cmd": "rate_order", "order_id": o['id'], "stars": i}))
                if i < 5:
                    kb.row()
        await message.answer(text, keyboard=kb.get_json() if kb else None)
        await asyncio.sleep(0.05)

# === Мои поездки ===
@bot.on.message(text="📈 Мои поездки")
async def driver_trips_list(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    if not await is_driver_registered(message.from_id):
        await message.answer("Сначала зарегистрируйтесь как водитель.", keyboard=kb_main(message.from_id))
        return
    async with _pg_pool.acquire() as conn:
        orders = await conn.fetch(
            "SELECT * FROM orders WHERE driver_id=$1 ORDER BY created_at DESC LIMIT 10",
            message.from_id
        )
    if not orders:
        await message.answer("У вас пока нет поездок.", keyboard=kb_driver_menu(await has_active_sub(message.from_id)))
        return
    for o in orders:
        kb = None
        if o['status'] == 'taken':
            kb = Keyboard(inline=True)
            kb.add(
                Text("❌ Отказаться от заказа", payload={"cmd": "driver_cancel", "order_id": o['id']}),
                color=KeyboardButtonColor.NEGATIVE
            )
        await message.answer(fmt_order(o), keyboard=kb.get_json() if kb else None)

# === Тарифы ===
@bot.on.message(text="📊 Тарифы")
async def show_tariffs(message: Message):
    if await check_blacklist(message.from_id):
        return await message.answer("⛔ Вы заблокированы.")
    text = "📊 Тарифы на поездки (₽/км):\n\n🇷🇺 РФ\n"
    for v in TARIFFS_RF.values():
        text += f"  {v['label']} — {v['price']} ₽/км\n"
    text += "\n🆕 Новые территории\n"
    for v in TARIFFS_NT.values():
        text += f"  {v['label']} — {v['price']} ₽/км\n"
    text += "\n⚠️ Платные дороги оплачиваются отдельно."
    await message.answer(text, keyboard=kb_main(message.from_id))

# === Абонемент ===
@bot.on.message(text="💳 Абонемент")
async def subscription_handler(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    has_sub = await has_active_sub(message.from_id)
    # FIX: text всегда инициализируется
    if has_sub:
        async with _pg_pool.acquire() as conn:
            r = await conn.fetchrow("SELECT expires_date FROM subscriptions WHERE user_id=$1", message.from_id)
        if r and r['expires_date']:
            expires_str = r['expires_date'].strftime('%d.%m.%Y') if hasattr(r['expires_date'], 'strftime') else str(r['expires_date'])
            text = f"💳 У вас есть активная подписка!\nДействует до: {expires_str}\n\nВы можете продлить подписку, выбрав тариф:"
        else:
            text = "💳 Выберите подписку для водителей:"
    else:
        text = "💳 Выберите подписку для водителей:"
    kb = Keyboard(inline=False)
    for key, plan in SUBS.items():
        kb.add(
            Text(plan['label'], payload={"cmd": "buy_sub", "plan_key": key}),
            color=KeyboardButtonColor.PRIMARY
        )
        kb.row()
    kb.add(Text("🔙 Главное меню"), color=KeyboardButtonColor.NEGATIVE)
    await message.answer(text, keyboard=kb.get_json())

# === Админ панель ===
@bot.on.message(text="🔧 Админ")
async def admin_panel(message: Message):
    if message.from_id not in ADMIN_IDS:
        return await message.answer("⛔ Доступ запрещён.")
    await message.answer("🔧 Административная панель:", keyboard=kb_admin_menu())

ADMIN_ORD_PAGE = 10  # заказов на страницу

async def _send_admin_orders_page(admin_id: int, page: int = 0):
    async with _pg_pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM orders")
        orders = await conn.fetch(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            ADMIN_ORD_PAGE, page * ADMIN_ORD_PAGE
        )
    if not orders and page == 0:
        await safe_send(admin_id, "📋 Заказов нет.")
        return
    if not orders:
        await safe_send(admin_id, "⚠️ Страница не найдена.")
        return
    total_pages = max(1, (total - 1) // ADMIN_ORD_PAGE + 1)
    await safe_send(admin_id, f"📋 Заказы — страница {page + 1} из {total_pages} (всего: {total})")
    for o in orders:
        text = fmt_order(o)
        text += f"\n👤 Пассажир: {vk_link(o['passenger_id'])}"
        if o['driver_id']:
            text += f"\n🚗 Водитель: {vk_link(o['driver_id'])}"
        kb = Keyboard(inline=True)
        if o['status'] == 'open':
            kb.add(Text("🔴 Отменить", payload={"cmd": "adm_cancel_order", "order_id": o['id']}), color=KeyboardButtonColor.NEGATIVE)
        elif o['status'] == 'taken':
            kb.add(Text("✅ Завершить", payload={"cmd": "adm_complete_order", "order_id": o['id']}), color=KeyboardButtonColor.POSITIVE)
            kb.add(Text("🔴 Отменить", payload={"cmd": "adm_cancel_order", "order_id": o['id']}), color=KeyboardButtonColor.NEGATIVE)
        await safe_send(admin_id, text, keyboard=kb.get_json() if o['status'] in ('open', 'taken') else None)
    # Навигация
    nav_kb = Keyboard(inline=True)
    if page > 0:
        nav_kb.add(Text("◀️ Назад", payload={"cmd": "adm_ord_page", "page": page - 1}), color=KeyboardButtonColor.SECONDARY)
    nav_kb.add(Text(f"· {page + 1}/{total_pages} ·", payload={"cmd": "adm_ord_noop"}), color=KeyboardButtonColor.SECONDARY)
    if (page + 1) * ADMIN_ORD_PAGE < total:
        nav_kb.add(Text("Вперёд ▶️", payload={"cmd": "adm_ord_page", "page": page + 1}), color=KeyboardButtonColor.SECONDARY)
    await safe_send(admin_id, "📄 Навигация:", keyboard=nav_kb.get_json())

@bot.on.message(text="📋 Заказы")
async def admin_orders(message: Message):
    if message.from_id not in ADMIN_IDS:
        return
    await _send_admin_orders_page(message.from_id, 0)

ADMIN_DRV_PAGE = 3  # водителей на страницу

async def _send_admin_drivers_page(admin_id: int, page: int = 0):
    async with _pg_pool.acquire() as conn:
        drivers = await conn.fetch(
            "SELECT d.*, COUNT(o.id) as trip_count, s.expires_date "
            "FROM drivers d "
            "LEFT JOIN orders o ON d.user_id = o.driver_id "
            "LEFT JOIN subscriptions s ON d.user_id = s.user_id "
            "GROUP BY d.user_id, s.expires_date ORDER BY d.registered_at DESC"
        )
    total = len(drivers)
    if not drivers:
        await safe_send(admin_id, "👥 Водителей нет.")
        return
    total_pages = max(1, (total - 1) // ADMIN_DRV_PAGE + 1)
    chunk = drivers[page * ADMIN_DRV_PAGE : (page + 1) * ADMIN_DRV_PAGE]
    if not chunk:
        await safe_send(admin_id, "⚠️ Страница не найдена.")
        return
    await safe_send(admin_id, f"👥 Водители — страница {page + 1} из {total_pages} (всего: {total})")
    for d in chunk:
        uid_d = d["user_id"]
        # Имя через VK API
        try:
            vk_u = await bot.api.users.get(user_ids=[uid_d])
            drv_name = f"{vk_u[0].first_name} {vk_u[0].last_name}" if vk_u else f"id{uid_d}"
        except Exception:
            drv_name = f"id{uid_d}"
        # Рейтинг
        avg, cnt = await avg_rating(uid_d)
        rat_txt = f"⭐ {avg}/5 ({cnt} оц.)" if cnt else "⭐ нет оценок"
        # Подписка — уже получена через JOIN в основном запросе
        exp = d.get("expires_date")
        if exp:
            dl = (exp - date.today()).days
            exp_str = exp.strftime('%d.%m.%Y') if hasattr(exp, 'strftime') else str(exp)
            sub_txt = f"✅ до {exp_str} ({dl} дн.)" if dl >= 0 else "❌ истекла"
        else:
            sub_txt = "❌ нет подписки"
        verf = "✅ Верифицирован" if d["docs_verified"] else "⏳ Ожидает"
        reg = str(d["registered_at"])[:10] if d.get("registered_at") else "—"
        cl = CLASS_LABELS.get(d["car_class"], d["car_class"])
        info = (
            f"👤 {drv_name} | ID: {uid_d}\n"
            f"🚘 {d['car_model']} ({d['car_year']})\n"
            f"🔢 {d['car_number']}\n"
            f"🏷 {cl}\n"
            f"📞 {d['phone']}\n"
            f"📄 {verf}\n"
            f"💳 {sub_txt}\n"
            f"{rat_txt}\n"
            f"🗓 Рег.: {reg}"
        )
        kb = Keyboard(inline=True)
        kb.add(OpenLink(label="💬 Написать", link=f"https://vk.com/id{uid_d}"))
        kb.row()
        kb.add(Text("✅ Верифицировать", payload={"cmd": "verify_driver", "user_id": uid_d}), color=KeyboardButtonColor.POSITIVE)
        kb.add(Text("❌ Снять вериф.", payload={"cmd": "reject_driver", "user_id": uid_d}), color=KeyboardButtonColor.NEGATIVE)
        kb.row()
        kb.add(Text("✏️ Редактировать", payload={"cmd": "adm_edit_drv", "user_id": uid_d}), color=KeyboardButtonColor.SECONDARY)
        kb.row()
        kb.add(Text("🗑 Удалить", payload={"cmd": "adm_del_drv", "user_id": uid_d}), color=KeyboardButtonColor.NEGATIVE)
        kb.add(Text("🚫 Бан", payload={"cmd": "adm_ban_drv", "user_id": uid_d}), color=KeyboardButtonColor.NEGATIVE)
        await safe_send(admin_id, info, keyboard=kb.get_json())
    # Навигация
    nav_kb = Keyboard(inline=True)
    if page > 0:
        nav_kb.add(Text("◀️ Назад", payload={"cmd": "adm_drv_page", "page": page - 1}), color=KeyboardButtonColor.SECONDARY)
    nav_kb.add(Text(f"· {page + 1}/{total_pages} ·", payload={"cmd": "adm_drv_noop"}), color=KeyboardButtonColor.SECONDARY)
    if (page + 1) * ADMIN_DRV_PAGE < total:
        nav_kb.add(Text("Вперёд ▶️", payload={"cmd": "adm_drv_page", "page": page + 1}), color=KeyboardButtonColor.SECONDARY)
    await safe_send(admin_id, "📄 Навигация:", keyboard=nav_kb.get_json())

@bot.on.message(text="👥 Пользователи")
async def admin_users(message: Message):
    if message.from_id not in ADMIN_IDS:
        return
    await _send_admin_drivers_page(message.from_id, 0)

@bot.on.message(text="✅ Верификация")
async def admin_verification(message: Message):
    if message.from_id not in ADMIN_IDS:
        return
    async with _pg_pool.acquire() as conn:
        unverified = await conn.fetch("SELECT * FROM drivers WHERE docs_verified=FALSE LIMIT 10")
    if not unverified:
        await message.answer("Нет водителей на верификацию.")
        return
    for d in unverified:
        reg_date = str(d['registered_at'])[:10] if d['registered_at'] else "—"
        text = (
            f"🚗 Водитель: {vk_link(d['user_id'])}\n"
            f"📱 Телефон: {d['phone']}\n"
            f"🚘 Авто: {d['car_model']} ({d['car_year']})\n"
            f"🔢 Номер: {d['car_number']}\n"
            f"🚘 Класс: {CLASS_LABELS.get(d['car_class'], d['car_class'])}\n"
            f"📅 Зарегистрирован: {reg_date}"
        )
        kb = Keyboard(inline=True)
        kb.add(
            Text("✅ Верифицировать", payload={"cmd": "verify_driver", "user_id": d['user_id']}),
            color=KeyboardButtonColor.POSITIVE
        ).row()
        kb.add(
            Text("❌ Отклонить", payload={"cmd": "reject_driver", "user_id": d['user_id']}),
            color=KeyboardButtonColor.NEGATIVE
        )
        await message.answer(text, keyboard=kb.get_json())

@bot.on.message(text="💳 Подписки")
async def admin_subscriptions(message: Message):
    if message.from_id not in ADMIN_IDS:
        return
    async with _pg_pool.acquire() as conn:
        pending = await conn.fetch("SELECT * FROM pending_subscriptions")
        active = await conn.fetch(
            "SELECT * FROM subscriptions WHERE expires_date >= $1",
            datetime.now(TZ).date()
        )
    text = "💳 Управление подписками:\n\n⏳ Ожидают активации:\n"
    if pending:
        for p in pending:
            plan = SUBS.get(p['plan_key'], {})
            text += f"{vk_link(p['user_id'])} | Тариф: {plan.get('label', p['plan_key'])}\n"
    else:
        text += "Нет ожидающих активации.\n"
    text += "\n✅ Активные подписки:\n"
    if active:
        for a in active:
            text += f"{vk_link(a['user_id'])} | До: {a['expires_date'].strftime('%d.%m.%Y') if hasattr(a['expires_date'], 'strftime') else a['expires_date']}\n"
    else:
        text += "Нет активных подписок.\n"
    await message.answer(text)

@bot.on.message(text="📊 Статистика")
async def admin_stats(message: Message):
    if message.from_id not in ADMIN_IDS:
        return
    async with _pg_pool.acquire() as conn:
        ord_stats = await conn.fetchrow(
            """SELECT
               COUNT(*) as total,
               COUNT(*) FILTER (WHERE status='completed') as completed,
               COUNT(*) FILTER (WHERE status='cancelled') as cancelled,
               COUNT(*) FILTER (WHERE status IN ('open','taken')) as active,
               COUNT(DISTINCT passenger_id) as passengers,
               COALESCE(SUM(price) FILTER (WHERE status='completed'), 0) as revenue
               FROM orders"""
        )
        drv_stats = await conn.fetchrow(
            """SELECT
               COUNT(*) as total,
               COUNT(*) FILTER (WHERE docs_verified=TRUE) as verified
               FROM drivers"""
        )
        active_subs = await conn.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE expires_date >= $1",
            datetime.now(TZ).date()
        )
    total_orders     = ord_stats["total"]
    completed_orders = ord_stats["completed"]
    cancelled_orders = ord_stats["cancelled"]
    active_orders    = ord_stats["active"]
    total_passengers = ord_stats["passengers"]
    total_revenue    = ord_stats["revenue"]
    total_drivers    = drv_stats["total"]
    verified_drivers = drv_stats["verified"]
    completion_rate = (completed_orders / total_orders * 100) if total_orders > 0 else 0
    text = (
        f"📊 Общая статистика:\n\n"
        f"📦 Всего заказов: {total_orders}\n"
        f"🟢 Активных: {active_orders}\n"
        f"✅ Выполнено: {completed_orders} ({completion_rate:.1f}%)\n"
        f"❌ Отменено: {cancelled_orders}\n"
        f"💰 Общая выручка: {total_revenue} ₽\n"
        f"👥 Пассажиров: {total_passengers}\n"
        f"🚗 Водителей: {total_drivers} (верифицировано: {verified_drivers})\n"
        f"💳 Активных подписок: {active_subs}"
    )
    await message.answer(text)

# === ЕДИНЫЙ PAYLOAD-ОБРАБОТЧИК ===
# Не используем @bot.on.message(payload={"cmd": "..."}) — vkbottle проверяет
# точное равенство dict, поэтому кнопки с доп. полями (user_id, order_id) не срабатывают.
@bot.on.message(func=lambda m: bool(m.payload))
async def unified_payload_handler(message: Message):
    try:
        data = parse_payload(message.payload)
        cmd = data.get("cmd")
        uid = message.from_id

        if not cmd:
            return

        # --- Админские команды ---
        if cmd == "verify_driver":
            if uid not in ADMIN_IDS:
                return await message.answer("⛔ Доступ запрещён.")
            target_uid = data.get("user_id")
            if not target_uid:
                log.warning(f"verify_driver: нет user_id в payload: {data!r}")
                return await message.answer("❌ Ошибка: не найден ID водителя.")
            async with _pg_pool.acquire() as conn:
                await conn.execute("UPDATE drivers SET docs_verified=TRUE WHERE user_id=$1", target_uid)
            await safe_send(target_uid, "✅ Ваш профиль водителя верифицирован! Теперь вы можете принимать заказы.")
            await message.answer(f"✅ Водитель {target_uid} верифицирован.")
            return

        elif cmd == "reject_driver":
            if uid not in ADMIN_IDS:
                return await message.answer("⛔ Доступ запрещён.")
            target_uid = data.get("user_id")
            if not target_uid:
                return await message.answer("❌ Ошибка: не найден ID водителя.")
            async with _pg_pool.acquire() as conn:
                await conn.execute("UPDATE drivers SET docs_verified=FALSE WHERE user_id=$1", target_uid)
            await safe_send(target_uid, "❌ Ваша заявка на регистрацию водителя отклонена администратором.")
            await message.answer(f"Заявка водителя {target_uid} отклонена.")
            return

        # --- Навигация по списку водителей ---
        elif cmd == "adm_drv_page":
            if uid not in ADMIN_IDS:
                return
            page = int(data.get("page", 0))
            await _send_admin_drivers_page(uid, page)
            return

        elif cmd == "adm_drv_noop":
            return

        elif cmd == "adm_ord_page":
            if uid not in ADMIN_IDS:
                return
            await _send_admin_orders_page(uid, int(data.get("page", 0)))
            return

        elif cmd == "adm_ord_noop":
            return

        # --- Редактирование водителя администратором ---
        elif cmd == "adm_edit_drv":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            async with _pg_pool.acquire() as conn:
                drv = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", target_uid)
            if not drv:
                return await message.answer("❌ Водитель не найден.")
            try:
                vk_u = await bot.api.users.get(user_ids=[target_uid])
                drv_name_str = f"{vk_u[0].first_name} {vk_u[0].last_name}" if vk_u else f"id{target_uid}"
            except Exception:
                drv_name_str = f"id{target_uid}"
            kb = Keyboard(inline=True)
            kb.add(Text("🚘 Марка/модель", payload={"cmd": "adm_ef", "user_id": target_uid, "field": "car_model"}), color=KeyboardButtonColor.SECONDARY)
            kb.row()
            kb.add(Text("📅 Год выпуска", payload={"cmd": "adm_ef", "user_id": target_uid, "field": "car_year"}), color=KeyboardButtonColor.SECONDARY)
            kb.row()
            kb.add(Text("🔢 Гос. номер", payload={"cmd": "adm_ef", "user_id": target_uid, "field": "car_number"}), color=KeyboardButtonColor.SECONDARY)
            kb.row()
            kb.add(Text("🏷 Класс авто", payload={"cmd": "adm_ef", "user_id": target_uid, "field": "car_class"}), color=KeyboardButtonColor.SECONDARY)
            await message.answer(f"✏️ Редактировать: {drv_name_str}\nЧто изменить?", keyboard=kb.get_json())
            return

        elif cmd == "adm_ef":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            field = data.get("field")
            async with _pg_pool.acquire() as conn:
                drv = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", target_uid)
            if not drv:
                return await message.answer("❌ Водитель не найден.")
            if field == "car_class":
                kb = Keyboard(inline=True)
                for k, lbl in CLASS_LABELS.items():
                    kb.add(Text(lbl, payload={"cmd": "adm_sc", "user_id": target_uid, "car_class": k}), color=KeyboardButtonColor.PRIMARY)
                    kb.row()
                await message.answer("🏷 Выберите новый класс:", keyboard=kb.get_json())
            else:
                prompts = {"car_model": "🚘 Введите новую марку и модель:", "car_year": "📅 Введите новый год:", "car_number": "🔢 Введите новый гос. номер:"}
                adm_edit_fsm[uid] = {"target_uid": target_uid, "field": field}
                await bot.state_dispenser.set(uid, AdminEditStates.waiting_input)
                await message.answer(prompts.get(field, "Введите значение:"), keyboard=kb_cancel())
            return

        elif cmd == "adm_sc":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            car_class = data.get("car_class")
            async with _pg_pool.acquire() as conn:
                await conn.execute("UPDATE drivers SET car_class=$1 WHERE user_id=$2", car_class, target_uid)
            await safe_send(target_uid, f"🏷 Администратор изменил класс вашего авто на: {CLASS_LABELS.get(car_class, car_class)}")
            await message.answer(f"✅ Класс авто обновлён: {CLASS_LABELS.get(car_class, car_class)}")
            return

        # --- Удаление водителя ---
        elif cmd == "adm_del_drv":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            async with _pg_pool.acquire() as conn:
                drv = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", target_uid)
            if not drv:
                return await message.answer("❌ Водитель не найден.")
            kb = Keyboard(inline=True)
            kb.add(Text("✅ Да, удалить", payload={"cmd": "adm_del_ok", "user_id": target_uid}), color=KeyboardButtonColor.NEGATIVE)
            kb.add(Text("❌ Нет", payload={"cmd": "adm_del_no"}), color=KeyboardButtonColor.SECONDARY)
            await message.answer(f"⚠️ Удалить водителя id{target_uid}?\n{drv['car_model']} | {drv['phone']}", keyboard=kb.get_json())
            return

        elif cmd == "adm_del_ok":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            async with _pg_pool.acquire() as conn:
                drv = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", target_uid)
                if drv:
                    # Освобождаем активные заказы
                    active = await conn.fetch(
                        "SELECT id, passenger_id FROM orders WHERE driver_id=$1 AND status='taken'", target_uid
                    )
                    for o in active:
                        await conn.execute("UPDATE orders SET status='open', driver_id=NULL WHERE id=$1", o["id"])
                        if o["passenger_id"]:
                            await safe_send(o["passenger_id"], f"⚠️ Водитель удалён администратором. Заказ #{o['id']} снова открыт.")
                    await conn.execute("DELETE FROM drivers WHERE user_id=$1", target_uid)
            await safe_send(target_uid, "🗑 Ваш профиль водителя удалён администратором.")
            await message.answer(f"✅ Водитель id{target_uid} удалён.")
            return

        elif cmd == "adm_del_no":
            await message.answer("❌ Удаление отменено.")
            return

        # --- Бан водителя ---
        elif cmd == "adm_ban_drv":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            async with _pg_pool.acquire() as conn:
                already = await conn.fetchrow("SELECT 1 FROM blacklist WHERE user_id=$1", target_uid)
            if already:
                # Уже в бане — предлагаем разбанить
                kb = Keyboard(inline=True)
                kb.add(Text("🔓 Разбанить", payload={"cmd": "adm_unban_drv", "user_id": target_uid}), color=KeyboardButtonColor.POSITIVE)
                await message.answer(f"ℹ️ id{target_uid} уже в чёрном списке.", keyboard=kb.get_json())
            else:
                async with _pg_pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO blacklist(user_id, reason) VALUES($1,$2) ON CONFLICT DO NOTHING",
                        target_uid, "Заблокирован администратором"
                    )
                await safe_send(target_uid, "🚫 Вы заблокированы администратором.")
                await message.answer(f"🚫 id{target_uid} добавлен в чёрный список.")
            return

        elif cmd == "adm_unban_drv":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            async with _pg_pool.acquire() as conn:
                await conn.execute("DELETE FROM blacklist WHERE user_id=$1", target_uid)
            await safe_send(target_uid, "✅ Вы разблокированы администратором.")
            await message.answer(f"✅ id{target_uid} удалён из чёрного списка.")
            return

        elif cmd == "activate_sub":
            if uid not in ADMIN_IDS:
                return await message.answer("⛔ Доступ запрещён.")
            target_uid = data.get("user_id")
            plan_key = data.get("plan_key")
            plan = SUBS.get(plan_key)
            if not plan or not target_uid:
                return await message.answer("❌ Ошибка данных.")
            async with _pg_pool.acquire() as conn:
                rec = await conn.fetchrow("SELECT expires_date FROM subscriptions WHERE user_id=$1", target_uid)
                base_date = datetime.now(TZ).date()
                if rec and rec['expires_date']:
                    try:
                        old_date = rec['expires_date'] if not isinstance(rec['expires_date'], str) \
                            else datetime.strptime(rec['expires_date'], "%Y-%m-%d").date()
                        if old_date > base_date:
                            base_date = old_date
                    except:
                        pass
                new_exp = base_date + timedelta(days=plan['days'])
                await conn.execute(
                    "INSERT INTO subscriptions(user_id, expires_date) VALUES($1,$2) "
                    "ON CONFLICT (user_id) DO UPDATE SET expires_date=EXCLUDED.expires_date",
                    target_uid, new_exp
                )
                await conn.execute("DELETE FROM pending_subscriptions WHERE user_id=$1", target_uid)
                await conn.execute(
                    "INSERT INTO subscription_log(user_id, target_user_id, plan_key, admin_id, action) "
                    "VALUES($1,$2,$3,$4,'activate')",
                    target_uid, target_uid, plan_key, uid
                )
            await safe_send(target_uid, f"🎉 Ваша подписка активирована до {new_exp.strftime('%d.%m.%Y')}!")
            await message.answer(f"✅ Подписка для {target_uid} активирована до {new_exp.strftime('%d.%m.%Y')}")
            return

        elif cmd == "reject_sub":
            if uid not in ADMIN_IDS:
                return await message.answer("⛔ Доступ запрещён.")
            target_uid = data.get("user_id")
            if not target_uid:
                return
            async with _pg_pool.acquire() as conn:
                await conn.execute("DELETE FROM pending_subscriptions WHERE user_id=$1", target_uid)
                await conn.execute(
                    "INSERT INTO subscription_log(user_id, target_user_id, admin_id, action) "
                    "VALUES($1,$2,$3,'reject')",
                    target_uid, target_uid, uid
                )
            await safe_send(target_uid, "❌ Ваша заявка на подписку отклонена администратором.")
            await message.answer(f"Заявка на подписку от {target_uid} отклонена.")
            return

        elif cmd == "adm_cancel_order":
            if uid not in ADMIN_IDS:
                return await message.answer("⛔ Доступ запрещён.")
            order_id = data.get("order_id")
            if not order_id:
                return
            order = await get_order(order_id)
            if not order:
                return await message.answer("Заказ не найден.")
            await update_order_status(order_id, "cancelled")
            await message.answer(f"❌ Заказ #{order_id} отменён.")
            if order['passenger_id']:
                await safe_send(order['passenger_id'], f"❌ Ваш заказ #{order_id} отменён администратором.")
            if order['driver_id']:
                await safe_send(order['driver_id'], f"❌ Заказ #{order_id} отменён администратором.")
            return

        elif cmd == "adm_complete_order":
            if uid not in ADMIN_IDS:
                return await message.answer("⛔ Доступ запрещён.")
            order_id = data.get("order_id")
            if not order_id:
                return
            order = await get_order(order_id)
            if not order:
                return await message.answer("Заказ не найден.")
            await update_order_status(order_id, "completed", driver_id=order['driver_id'])
            await message.answer(f"✅ Заказ #{order_id} завершён.")
            if order['passenger_id']:
                kb = Keyboard(inline=True)
                for i in range(1, 6):
                    kb.add(Text(f"{i}⭐", payload={"cmd": "rate_order", "order_id": order_id, "stars": i}))
                    if i < 5:
                        kb.row()
                await safe_send(
                    order['passenger_id'],
                    f"✅ Ваш заказ #{order_id} завершён.\n\nПожалуйста, оцените поездку:",
                    keyboard=kb.get_json()
                )
            if order['driver_id']:
                await safe_send(order['driver_id'], f"✅ Заказ #{order_id} завершён администратором.")
            return

        # --- Пользовательские команды ---
        if await check_blacklist(uid) and uid not in ADMIN_IDS:
            await message.answer("⛔ Вы заблокированы.")
            return

        if cmd == "take_order":
            order_id = data.get("order_id")
            if not await is_driver_verified(uid):
                return await message.answer("❌ Ваш профиль не верифицирован.")
            if not await has_active_sub(uid):
                return await message.answer("❌ Нет активной подписки.")
            if await is_driver_busy(uid):
                return await message.answer("❌ У вас уже есть активный заказ.")
            async with _pg_pool.acquire() as conn:
                driver_data = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", uid)
            if not driver_data:
                return await message.answer("❌ Профиль водителя не найден.")
            success, error_msg, order = await try_take_order_atomic(order_id, uid, driver_data['car_class'])
            if not success:
                return await message.answer(f"❌ {error_msg}")
            avg, cnt = await avg_rating(uid)
            rating_text = f"\n⭐ Рейтинг: {avg}/5 ({cnt} оценок)" if cnt else "\n⭐ Новый водитель"
            passenger_id = order['passenger_id']
            # Ссылки на профили ВК
            purl = f"https://vk.com/id{passenger_id}"
            durl = f"https://vk.com/id{uid}"
            # Уведомление водителю
            drv_kb = Keyboard(inline=True)
            drv_kb.add(OpenLink(label="💬 Написать пассажиру", link=purl))
            drv_kb.row()
            drv_kb.add(Text("❌ Отказаться от заказа", payload={"cmd": "driver_cancel", "order_id": order_id}), color=KeyboardButtonColor.NEGATIVE)
            car_class_label = CLASS_LABELS.get(driver_data["car_class"], driver_data["car_class"])
            await message.answer(
                f"✅ Заказ #{order_id} принят!\n"
                f"👤 Пассажир: vk.com/id{passenger_id}\n"
                f"📍 {esc(order['from_city'])} → {esc(order['to_city'])}\n"
                f"📅 {order['trip_date'].strftime('%d.%m.%Y') if hasattr(order['trip_date'], 'strftime') else order['trip_date']} "
                f"🕐 {order['trip_time'].strftime('%H:%M') if hasattr(order['trip_time'], 'strftime') else order['trip_time']}\n"
                f"👥 Пассажиров: {order['passengers']}",
                keyboard=drv_kb.get_json()
            )
            # Получаем имя водителя через VK API
            try:
                vk_users = await bot.api.users.get(user_ids=[uid])
                driver_name = f"{vk_users[0].first_name} {vk_users[0].last_name}" if vk_users else f"vk.com/id{uid}"
            except Exception:
                driver_name = f"vk.com/id{uid}"
            # Уведомление пассажиру
            pass_kb = Keyboard(inline=True)
            pass_kb.add(OpenLink(label="📞 Написать водителю", link=durl))
            pass_kb.row()
            pass_kb.add(Text("✅ Завершить поездку", payload={"cmd": "done_order", "order_id": order_id}), color=KeyboardButtonColor.POSITIVE)
            pass_kb.row()
            pass_kb.add(Text("❌ Отменить заказ", payload={"cmd": "cancel_order", "order_id": order_id}), color=KeyboardButtonColor.NEGATIVE)
            car_note = (
                f"\n⚠️ Водитель приедет на {car_class_label}"
                if driver_data["car_class"] != order["car_class"] else ""
            )
            await safe_send(
                passenger_id,
                f"🎉 Водитель найден!\n"
                f"👤 {esc(driver_name)}{rating_text}\n"
                f"🚘 {esc(driver_data['car_model'])} ({driver_data['car_year']})\n"
                f"🔢 {esc(driver_data['car_number'])}\n"
                f"📞 {esc(driver_data['phone'])}{car_note}\n\n"
                f"⏳ Ожидайте — с вами свяжется водитель.\n\n"
                f"🚫 Не переводите предоплату!\n"
                f"Оплата — только водителю после поездки.",
                keyboard=pass_kb.get_json()
            )
            return

        elif cmd == "skip_order":
            await message.answer("Вы пропустили этот заказ.")
            return

        elif cmd == "done_order":
            order_id = data.get("order_id")
            order = await get_order(order_id)
            if not order or order['passenger_id'] != uid:
                return await message.answer("❌ Ошибка: этот заказ не ваш.")
            if order['status'] != 'taken':
                return await message.answer("❌ Заказ не в работе.")
            await update_order_status(order_id, 'completed')
            # Уведомление водителю
            if order['driver_id']:
                await safe_send(order['driver_id'], f"✅ Поездка #{order_id} завершена пассажиром. Спасибо за работу!")
            # Клавиатура со звёздами — 5 кнопок в один ряд
            stars_kb = Keyboard(inline=True)
            for i in range(1, 6):
                stars_kb.add(
                    Text(f"{i}⭐", payload={"cmd": "rate_order", "order_id": order_id, "stars": i}),
                    color=KeyboardButtonColor.PRIMARY
                )
            await message.answer(
                f"✅ Поездка завершена!\n\n"
                f"⭐ Пожалуйста, оцените водителя:\n"
                f"(нажмите на количество звёзд)",
                keyboard=stars_kb.get_json()
            )
            return

        elif cmd == "cancel_order":
            order_id = data.get("order_id")
            order = await get_order(order_id)
            if not order or order['passenger_id'] != uid:
                return await message.answer("❌ Ошибка: этот заказ не ваш.")
            if order['status'] not in ['open', 'taken']:
                return await message.answer("❌ Заказ нельзя отменить.")
            await update_order_status(order_id, 'cancelled')
            await message.answer(f"❌ Заказ #{order_id} отменён.")
            if order['driver_id']:
                await safe_send(order['driver_id'], f"❌ Пассажир отменил заказ #{order_id}.")
            return

        elif cmd == "driver_cancel":
            order_id = data.get("order_id")
            order = await get_order(order_id)
            if not order or order['driver_id'] != uid:
                return await message.answer("❌ Вы не водитель этого заказа.")
            if order['status'] != 'taken':
                return await message.answer("❌ Заказ нельзя отменить.")
            await update_order_status(order_id, 'open', clear_driver=True)
            await message.answer(f"Вы отказались от заказа #{order_id}.")
            await safe_send(order['passenger_id'], f"⚠️ Водитель отказался от заказа #{order_id}.")
            asyncio.create_task(notify_drivers_about_order(order_id, exclude_drivers=[uid]))
            return

        elif cmd == "rate_order":
            order_id = data.get("order_id")
            stars = int(data.get("stars", 0))
            order = await get_order(order_id)
            if not order or order['passenger_id'] != uid:
                return await message.answer("❌ Ошибка: этот заказ не ваш.")
            if order['status'] != 'completed':
                return await message.answer("❌ Поездка ещё не завершена.")
            if stars < 1 or stars > 5:
                return await message.answer("❌ Оценка должна быть от 1 до 5.")
            # Проверка повторной оценки
            if await has_rating(order_id, uid):
                return await message.answer("Вы уже оценили эту поездку.")
            drv_id = order['driver_id']
            if not drv_id:
                return await message.answer("❌ Водитель не найден.")
            await add_rating(order_id, drv_id, uid, stars)
            avg, cnt = await avg_rating(drv_id)
            stars_display = "⭐" * stars + "☆" * (5 - stars)
            await message.answer(
                f"{stars_display} Ваша оценка: {stars} из 5\n"
                f"✅ Спасибо! Оценка сохранена."
            )
            # Уведомление водителю с новым рейтингом
            await safe_send(
                drv_id,
                f"⭐ Новая оценка за поездку #{order_id}!\n"
                f"Пассажир поставил: {stars_display}\n"
                f"Ваш средний рейтинг: {avg}/5 ({cnt} оценок)"
            )
            return

        elif cmd == "buy_sub":
            if not await is_driver_registered(uid):
                return await message.answer("❌ Сначала зарегистрируйтесь как водитель.")
            plan_key = data.get("plan_key")
            plan = SUBS.get(plan_key)
            if not plan:
                return await message.answer("❌ Ошибка тарифного плана.")
            async with _pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO pending_subscriptions(user_id, plan_key) VALUES($1,$2) "
                    "ON CONFLICT (user_id) DO UPDATE SET plan_key=$2, created_at=NOW()",
                    uid, plan_key
                )
            for admin_id in ADMIN_IDS:
                kb = Keyboard(inline=True)
                kb.add(
                    Text("✅ Активировать", payload={"cmd": "activate_sub", "user_id": uid, "plan_key": plan_key}),
                    color=KeyboardButtonColor.POSITIVE
                ).row()
                kb.add(
                    Text("❌ Отклонить", payload={"cmd": "reject_sub", "user_id": uid}),
                    color=KeyboardButtonColor.NEGATIVE
                )
                await safe_send(
                    admin_id,
                    f"💳 Заявка на подписку от ID {uid}:\nТариф: {plan['label']}",
                    keyboard=kb.get_json()
                )
            await message.answer(f"✅ Заявка на подписку '{plan['label']}' отправлена администратору.")
            return

    except Exception as e:
        log.error(f"Ошибка в unified_payload_handler: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")

# === Редактирование водителя администратором (ввод нового значения) ===
@bot.on.message(state=AdminEditStates.waiting_input)
async def adm_edit_input(message: Message):
    uid = message.from_id
    if uid not in ADMIN_IDS:
        return
    edit_data = adm_edit_fsm.pop(uid, None)
    if not edit_data:
        await bot.state_dispenser.delete(uid)
        return await message.answer("❌ Сессия редактирования истекла.")
    target_uid = edit_data["target_uid"]
    field = edit_data["field"]
    text = (message.text or "").strip()
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(uid)
        return await message.answer("❌ Редактирование отменено.", keyboard=kb_admin_menu())
    async with _pg_pool.acquire() as conn:
        if field == "car_model":
            if len(text) < 2 or len(text) > 100:
                adm_edit_fsm[uid] = edit_data
                return await message.answer("❌ Введите марку и модель (2–100 символов):")
            await conn.execute("UPDATE drivers SET car_model=$1 WHERE user_id=$2", text, target_uid)
            await safe_send(target_uid, f"🚘 Администратор изменил марку/модель вашего авто: {text}")
        elif field == "car_year":
            try:
                year = int(text)
                if year < 2008 or year > 2030:
                    adm_edit_fsm[uid] = edit_data
                    return await message.answer("❌ Год должен быть от 2008 до 2030:")
                await conn.execute("UPDATE drivers SET car_year=$1 WHERE user_id=$2", year, target_uid)
                await safe_send(target_uid, f"📅 Администратор изменил год выпуска вашего авто: {year}")
            except ValueError:
                adm_edit_fsm[uid] = edit_data
                return await message.answer("❌ Введите корректный год:")
        elif field == "car_number":
            number = text.upper().replace(" ", "")
            if not number:
                adm_edit_fsm[uid] = edit_data
                return await message.answer("❌ Введите номер:")
            await conn.execute("UPDATE drivers SET car_number=$1 WHERE user_id=$2", number, target_uid)
            await safe_send(target_uid, f"🔢 Администратор изменил гос. номер вашего авто: {number}")
    await bot.state_dispenser.delete(uid)
    await message.answer(f"✅ Поле обновлено: {esc(text)}", keyboard=kb_admin_menu())

# === Команды бана/разбана ===
@bot.on.message(func=lambda m: m.text and m.text.startswith("/ban "))
async def admin_ban(message: Message):
    if message.from_id not in ADMIN_IDS:
        return await message.answer("⛔ Доступ запрещён.")
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /ban ID [причина]")
        return
    target_id = int(parts[1])
    reason = parts[2] if len(parts) > 2 else "Не указана"
    async with _pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO blacklist(user_id, reason) VALUES($1,$2) ON CONFLICT DO NOTHING",
            target_id, reason
        )
    await message.answer(f"🚫 Пользователь {target_id} заблокирован.")
    await safe_send(target_id, f"⛔ Вы были заблокированы администратором.\nПричина: {reason}")

@bot.on.message(func=lambda m: m.text and m.text.startswith("/unban "))
async def admin_unban(message: Message):
    if message.from_id not in ADMIN_IDS:
        return await message.answer("⛔ Доступ запрещён.")
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /unban ID")
        return
    target_id = int(parts[1])
    async with _pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM blacklist WHERE user_id=$1", target_id)
    await message.answer(f"✅ Пользователь {target_id} разблокирован.")
    await safe_send(target_id, "✅ Ваша блокировка снята администратором.")

# === Профиль водителя ===
@bot.on.message(text="👤 Мой профиль")
async def driver_profile(message: Message):
    if await check_blacklist(message.from_id):
        return await message.answer("⛔ Вы заблокированы.")
    if not await is_driver_registered(message.from_id):
        return await message.answer("Вы не зарегистрированы как водитель.", keyboard=kb_main(message.from_id))
    async with _pg_pool.acquire() as conn:
        driver = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", message.from_id)
    if not driver:
        return await message.answer("Профиль не найден.", keyboard=kb_main(message.from_id))
    has_sub = await has_active_sub(message.from_id)
    avg, cnt = await avg_rating(message.from_id)
    is_busy = await is_driver_busy(message.from_id)
    reg_date = str(driver['registered_at'])[:10] if driver['registered_at'] else "—"
    text = (
        f"👤 Профиль водителя:\n\n"
        f"📱 Телефон: {driver['phone']}\n"
        f"🚗 Автомобиль: {driver['car_model']} ({driver['car_year']})\n"
        f"🔢 Номер: {driver['car_number']}\n"
        f"🚘 Класс: {CLASS_LABELS.get(driver['car_class'], driver['car_class'])}\n"
        f"✅ Верификация: {'Да' if driver['docs_verified'] else 'Нет'}\n"
        f"💳 Подписка: {'Активна' if has_sub else 'Отсутствует'}\n"
        f"📊 Статус: {'Занят' if is_busy else 'Свободен'}\n"
        f"⭐ Рейтинг: {avg}/5 ({cnt} оценок)\n"
        f"📅 Зарегистрирован: {reg_date}"
    )
    await message.answer(text, keyboard=kb_driver_menu(has_sub))

# === Graceful shutdown ===
async def shutdown():
    log.info("Завершение работы...")
    # Закрываем aiohttp-сессию самого vkbottle чтобы избежать
    # "Unclosed client session" в логах при перезапуске
    try:
        if hasattr(bot, 'api') and hasattr(bot.api, 'http_client'):
            await bot.api.http_client.close()
    except Exception as e:
        log.warning(f"Ошибка при закрытии http_client: {e}")
    if _pg_pool:
        await _pg_pool.close()
    log.info("Бот остановлен.")

async def main():
    await init_pg()
    # Регистрируем middleware для восстановления FSM ДО роутинга
    bot.labeler.message_view.register_middleware(FSMRestoreMiddleware)
    asyncio.create_task(fsm_cleanup_task())
    asyncio.create_task(auto_cancel_expired_orders())
    log.info("🚀 Бот Межгород Трансфер Россия (ВК) успешно запущен!")
    try:
        await bot.run_polling()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бот остановлен пользователем")
