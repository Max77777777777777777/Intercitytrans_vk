# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import os
import re
import random
import secrets
import aiohttp
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
DISPATCHER_PHONES = ["+79033176800", "+79381584161"]

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
    "standard":    {"label": "🚗 Стандарт",  "price": 25,   "price_seat": 11},
    "comfort":     {"label": "🚙 Комфорт",   "price": 34,   "price_seat": 15},
    "comfort_plus":{"label": "✨ Комфорт+",  "price": 40,   "price_seat": 17.5},
    "minivan":     {"label": "🚐 Минивэн",   "price": 45,   "price_seat": 10},
    "business":    {"label": "💼 Бизнес",    "price": 60,   "price_seat": 26},
}
TARIFFS_NT = {
    "standard":    {"label": "🚗 Стандарт",  "price": 40,   "price_seat": 17.5},
    "comfort":     {"label": "🚙 Комфорт",   "price": 50,   "price_seat": 21.5},
    "comfort_plus":{"label": "✨ Комфорт+",  "price": 58,   "price_seat": 25},
    "minivan":     {"label": "🚐 Минивэн",   "price": 65,   "price_seat": 14},
    "business":    {"label": "💼 Бизнес",    "price": 80,   "price_seat": 34.5},
}
TARIFFS_CIS = {
    "standard":    {"label": "🚗 Стандарт",  "price": 32,   "price_seat": 12.5},
    "comfort":     {"label": "🚙 Комфорт",   "price": 37,   "price_seat": 15.5},
    "comfort_plus":{"label": "✨ Комфорт+",  "price": 45,   "price_seat": 17.5},
    "minivan":     {"label": "🚐 Минивэн",   "price": 55,   "price_seat": 15.5},
    "business":    {"label": "💼 Бизнес",    "price": 60,   "price_seat": 25},
}

CIS_KW = [
    # Грузия
    "тбилиси","батуми","кутаиси","рустави","гори","зугдиди","поти","телави","мцхета","боржоми","сигнахи","грузия",
    # Армения
    "ереван","гюмри","ванадзор","вагаршапат","абовян","армения",
    # Азербайджан
    "баку","гянджа","сумгаит","мингячевир","нахчыван","азербайджан",
    # Казахстан
    "алматы","астана","шымкент","актобе","тараз","павлодар","казахстан",
    # Беларусь
    "минск","гомель","могилёв","витебск","гродно","брест","беларусь",
    # Узбекистан
    "ташкент","самарканд","бухара","наманган","андижан","узбекистан",
    # Кыргызстан
    "бишкек","ош","джалал-абад","кыргызстан",
    # Таджикистан
    "душанбе","худжанд","таджикистан",
    # Туркменистан
    "ашхабад","туркменистан",
    # Молдова
    "кишинёв","тирасполь","молдова",
    # Турция
    "стамбул","анкара","трабзон","эрзурум","карс","турция",
    # Иран
    "тебриз","ардебиль",
]

CLASS_LABELS = {
    "standard": "Стандарт", "comfort": "Комфорт",
    "comfort_plus": "Комфорт+", "minivan": "Минивэн", "business": "Бизнес"
}
CAR_CLASS_TIER = {"standard": 0, "comfort": 1, "comfort_plus": 2, "business": 3}

REGION_LABELS = {"rf": "🇷🇺 Россия", "nt": "🆕 Новые территории", "cis": "🌍 Кавказ/СНГ"}

def is_nt(city: str) -> bool:
    city_l = city.lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", city_l) for kw in NT_KW)

def is_cis(city: str) -> bool:
    city_l = city.lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", city_l) for kw in CIS_KW)

def route_region(from_city: str, to_city: str) -> str:
    """Приоритет: НТ > Кавказ/СНГ > РФ"""
    if is_nt(from_city) or is_nt(to_city):   return "nt"
    if is_cis(from_city) or is_cis(to_city): return "cis"
    return "rf"

def tariffs(cf: str, ct: str):
    r = route_region(cf, ct)
    if r == "nt":  return TARIFFS_NT
    if r == "cis": return TARIFFS_CIS
    return TARIFFS_RF

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

class TripStates(BaseStateGroup):
    from_city      = "trip_from_city"
    to_city        = "trip_to_city"
    trip_date      = "trip_trip_date"
    trip_time      = "trip_trip_time"
    seats_total    = "trip_seats_total"
    car_class      = "trip_car_class"
    price_per_seat = "trip_price_per_seat"

class SearchTripStates(BaseStateGroup):
    from_city = "srch_from_city"
    to_city   = "srch_to_city"
    trip_date = "srch_trip_date"

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
    "trip_from_city":      TripStates.from_city,
    "trip_to_city":        TripStates.to_city,
    "trip_trip_date":      TripStates.trip_date,
    "trip_trip_time":      TripStates.trip_time,
    "trip_seats_total":    TripStates.seats_total,
    "trip_car_class":      TripStates.car_class,
    "trip_price_per_seat": TripStates.price_per_seat,
    "srch_from_city": SearchTripStates.from_city,
    "srch_to_city":   SearchTripStates.to_city,
    "srch_trip_date": SearchTripStates.trip_date,
}

# === Клавиатуры ===
def kb_main(user_id=None):
    kb = Keyboard(inline=False)
    kb.add(Text("🚕 Создать заказ"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("🚗 Я водитель"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("🚐 Поехать вместе"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("🎫 Мои брони"), color=KeyboardButtonColor.SECONDARY).row()
    kb.add(Text("📋 Мои заказы"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("📊 Тарифы"), color=KeyboardButtonColor.SECONDARY)
    if user_id in ADMIN_IDS:
        kb.row()
        kb.add(Text("🔧 Админ"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_driver_menu(has_sub: bool):
    kb = Keyboard(inline=False)
    kb.add(Text("📦 Доступные заказы"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("🚐 Создать рейс"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("🗓 Мои рейсы"), color=KeyboardButtonColor.SECONDARY)
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
                        (NOW() AT TIME ZONE 'UTC') + INTERVAL '1 hour' * $1
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
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                cancel_token TEXT
            )
        """)
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS cancel_token TEXT")
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
        # ── РЕЙСЫ ──
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trips(
                id SERIAL PRIMARY KEY,
                driver_id BIGINT NOT NULL,
                from_city TEXT NOT NULL,
                to_city TEXT NOT NULL,
                trip_date DATE NOT NULL,
                trip_time TIME NOT NULL,
                car_class TEXT NOT NULL,
                car_class_label TEXT NOT NULL,
                seats_total INT NOT NULL,
                seats_free INT NOT NULL,
                price_per_seat INT NOT NULL,
                distance_km REAL,
                region TEXT NOT NULL DEFAULT 'rf',
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN ('open','full','cancelled','done')),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trip_bookings(
                id SERIAL PRIMARY KEY,
                trip_id INT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
                passenger_id BIGINT NOT NULL,
                seats INT NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'confirmed'
                    CHECK(status IN ('confirmed','cancelled_by_driver','cancelled_by_passenger')),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(trip_id, passenger_id)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trips_driver  ON trips(driver_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trips_status  ON trips(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_trips_route   ON trips(from_city, to_city, trip_date)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_trip ON trip_bookings(trip_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_pass ON trip_bookings(passenger_id)")
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

async def _get_fsm_data(uid: int) -> dict:
    """Получает данные FSM — сначала из RAM, при отсутствии из БД.
    Это защита от потери данных при перезапуске бота во время заполнения формы."""
    if uid in fsm_data and fsm_data[uid]:
        return fsm_data[uid]
    # RAM пуста (перезапуск) — читаем из БД
    _, db_data = await load_fsm(uid)
    if db_data:
        fsm_data[uid] = db_data
        fsm_data_ts[uid] = asyncio.get_event_loop().time()
        log.info(f"FSM данные восстановлены из БД для uid={uid}: {list(db_data.keys())}")
    return fsm_data.get(uid, {})

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

def fmt_dt_ru(dt, with_time=False):
    """datetime/date -> 'ДД.ММ.ГГГГ' (или 'ДД.ММ.ГГГГ ЧЧ:ММ' при with_time=True).
    Не datetime/date или пусто -> '—'."""
    if not dt:
        return "—"
    if hasattr(dt, "strftime"):
        return dt.strftime("%d.%m.%Y %H:%M") if with_time else dt.strftime("%d.%m.%Y")
    return str(dt)

def vk_link(user_id, label=None):
    """Кликабельное упоминание профиля ВК в формате [id123|имя]"""
    label = label or f"ID {user_id}"
    return f"[id{user_id}|{label}]"

def yandex_maps_url(from_city: str, to_city: str) -> str:
    """Ссылка на маршрут в Яндекс.Картах."""
    import urllib.parse
    f = urllib.parse.quote(from_city)
    t = urllib.parse.quote(to_city)
    return f"https://yandex.ru/maps/?rtext={f}~{t}&rtt=auto"

def is_valid_city(city):
    return bool(city) and len(city.strip()) >= 2

_DATE_RE = re.compile(r"^(\d{1,2})[.\-/\s]*(\d{1,2})[.\-/\s]*(\d{2,4})$")
_TIME_RE = re.compile(r"^(\d{1,2})[:.\-\s]*(\d{2})$")

def parse_ru_date(raw):
    """
    Гибкий парсинг даты: ДД.ММ.ГГГГ и щадящие варианты — без точек (02072026),
    с -/./пробелом вместо точек, двузначный год (26 -> 2026).
    Возвращает date в диапазоне [сегодня; сегодня+365] или None (неверный
    формат/несуществующая дата/дата вне диапазона, в т.ч. raw=None).
    """
    text = (raw or "").strip()
    m = _DATE_RE.match(text)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    today = datetime.now(TZ).date()
    if not (today <= d <= today + timedelta(days=365)):
        return None
    return d

def parse_ru_time(raw):
    """
    Гибкий парсинг времени: ЧЧ:ММ и щадящие варианты — без двоеточия (1430),
    с точкой/тире/пробелом. Возвращает (час, минута) или None при неверном
    формате/диапазоне (в т.ч. raw=None).
    """
    text = (raw or "").strip()
    m = _TIME_RE.match(text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h, mi

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

async def validate_city_geocode(city: str) -> tuple[bool, bool]:
    """Проверяет город через геокодер.
    Возвращает (city_valid, geocoder_available).
    Если геокодер недоступен — возвращаем (True, False) чтобы не блокировать пользователя."""
    if not geolocator:
        return True, False
    try:
        result = await asyncio.wait_for(geocode_async(city), timeout=6.0)
        return result is not None, True
    except asyncio.TimeoutError:
        log.warning(f"Геокодер таймаут для '{city}' — пропускаем валидацию")
        return True, False
    except Exception as e:
        log.warning(f"Геокодер недоступен для '{city}': {e} — пропускаем валидацию")
        return True, False

async def _osrm_distance(coords_from: tuple, coords_to: tuple) -> int | None:
    """Реальное расстояние по дорогам через публичный OSRM.
    Возвращает км или None если сервис недоступен."""
    try:
        lat1, lon1 = coords_from
        lat2, lon2 = coords_to
        url = (
            f"https://router.project-osrm.org/route/v1/driving/"
            f"{lon1},{lat1};{lon2},{lat2}?overview=false"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == "Ok":
                        meters = data["routes"][0]["distance"]
                        return round(meters / 1000)
    except Exception as e:
        log.warning(f"OSRM недоступен: {e}")
    return None

async def calculate_distance_async(city_from: str, city_to: str) -> int | None:
    """Считает расстояние по маршруту:
    1. Геокодируем оба города параллельно
    2. Пробуем OSRM (реальная дорога)
    3. Fallback — геодезическое × региональный коэффициент"""
    coords_from, coords_to = await asyncio.gather(
        geocode_async(city_from),
        geocode_async(city_to)
    )
    if not coords_from or not coords_to:
        return None

    # Пробуем OSRM
    osrm_km = await _osrm_distance(coords_from, coords_to)
    if osrm_km:
        log.info(f"OSRM: {city_from}→{city_to} = {osrm_km} км")
        return osrm_km

    # Fallback: геодезическое × коэффициент (зависит от региона)
    region = route_region(city_from, city_to)
    coeff = 1.45 if region == "cis" else (1.35 if region == "nt" else 1.25)
    km = round(geo_dist(coords_from, coords_to).kilometers * coeff)
    log.info(f"Геодезическое (coeff={coeff}): {city_from}→{city_to} = {km} км")
    return km

def calculate_price(distance_km, car_class, from_city="", to_city="", trip_mode=False):
    """trip_mode=True — цена за одно место в рейсе (price_seat), иначе — вся машина (price)."""
    if distance_km is None or not isinstance(distance_km, (int, float)) or distance_km <= 0:
        return None
    if not car_class or not isinstance(car_class, str):
        return None
    t = tariffs(from_city, to_city)
    entry = t.get(car_class)
    if entry is None:
        # Неизвестный класс — fallback на стандарт с логированием
        log.warning(f"calculate_price: неизвестный car_class={car_class!r}, fallback на standard")
        entry = t.get("standard", {"price": 25, "price_seat": 11})
    rate = entry.get("price_seat" if trip_mode else "price")
    if not rate:
        return None
    return round(distance_km * rate)

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
        'open': '🟢 Открыт', 'taken': '🟡 Принят',
        'completed': '✅ Завершён', 'cancelled': '❌ Отменён'
    }
    distance_str = f"{int(o.get('distance_km'))} км" if o.get('distance_km') is not None else "—"
    price_str = f"{o.get('price')} ₽" if o.get('price') is not None else "—"
    region = route_region(o.get('from_city',''), o.get('to_city',''))
    region_label = REGION_LABELS.get(region, "🇷🇺 Россия")
    lines = [
        f"🚕 Заказ #{o['id']} · {region_label}",
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

def fmt_trip(t):
    """Форматирует рейс для отображения."""
    region_label = REGION_LABELS.get(t.get("region","rf"), "🇷🇺 Россия")
    seats_total = t.get("seats_total", 0)
    seats_free  = t.get("seats_free", 0)
    seats_taken = seats_total - seats_free
    trip_date = t['trip_date'].strftime('%d.%m.%Y') if hasattr(t.get('trip_date'), 'strftime') else str(t.get('trip_date',''))
    trip_time = t['trip_time'].strftime('%H:%M') if hasattr(t.get('trip_time'), 'strftime') else str(t.get('trip_time',''))
    status_map = {"open":"🟢 Открыт","full":"🔵 Набран","cancelled":"❌ Отменён","done":"✅ Завершён"}
    dist = t.get("distance_km")
    dist_str = f"{int(dist)} км" if dist else "—"
    lines = [
        f"🚐 Рейс #{t['id']} · {region_label}",
        f"📍 {esc(t.get('from_city'))} → {esc(t.get('to_city'))}",
        f"📅 {trip_date} · 🕐 {trip_time}",
        f"📏 {dist_str}",
        f"🚘 {esc(t.get('car_class_label'))}",
        f"💺 Мест: {seats_taken}/{seats_total} занято · своб.: {seats_free}",
        f"💰 {t.get('price_per_seat', 0)} ₽/место",
        f"Статус: {status_map.get(t.get('status','open'), t.get('status',''))}",
    ]
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
        import traceback
        log.error(f"Ошибка отправки пользователю {user_id}: {e}\n{''.join(traceback.format_stack())}")

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
    uid = message.from_id
    if await check_blacklist(message.from_id):
        await safe_send(uid, "⛔ Вы заблокированы.")
        return
    uid = message.from_id
    try:
        await bot.state_dispenser.delete(uid)
    except KeyError:
        pass
    fsm_data.pop(uid, None)
    await delete_fsm(uid)
    await safe_send(uid, 
        "🚕 Добро пожаловать в Межгород Трансфер!\n\n"
        "Я помогу найти попутную машину или заказать поездку между городами.\n\n"
        "Выберите действие в меню:",
        keyboard=kb_main(uid)
    )

# ================= СОЗДАНИЕ ЗАКАЗА (FSM) =================
@bot.on.message(text="🚕 Создать заказ")
async def start_order(message: Message):
    uid = message.from_id
    if await check_blacklist(message.from_id):
        await safe_send(uid, "⛔ Вы заблокированы.")
        return
    uid = message.from_id
    await bot.state_dispenser.set(uid, OrderStates.from_city)
    fsm_data[uid] = {}
    await save_fsm(uid, OrderStates.from_city, {})
    await safe_send(uid, "📍 Введите город отправления:", keyboard=kb_cancel())

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
        await safe_send(uid, "Создание заказа отменено.", keyboard=kb_main(uid))
        return
    if not is_valid_city(message.text):
        await safe_send(uid, "❌ Некорректное название города. Введите ещё раз:")
        return
    city = (message.text or "").strip()
    city_ok, geo_available = await validate_city_geocode(city)
    if not city_ok:
        await safe_send(uid, "❌ Город не найден на карте. Проверьте название и введите ещё раз:")
        return
    fsm_data.setdefault(uid, {})["from_city"] = city
    await bot.state_dispenser.set(uid, OrderStates.to_city)
    await save_fsm(uid, OrderStates.to_city, fsm_data[uid])
    await safe_send(uid, "📍 Введите город назначения:")

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
        await safe_send(uid, "Создание заказа отменено.", keyboard=kb_main(uid))
        return
    if not is_valid_city(message.text):
        await safe_send(uid, "❌ Некорректное название города. Введите ещё раз:")
        return
    data = await _get_fsm_data(uid)
    from_city = data.get("from_city", "")
    city = (message.text or "").strip()
    if city.lower() == from_city.lower():
        await safe_send(uid, "❌ Город отправления и назначения не должны совпадать. Введите другой город:")
        return
    city_ok, geo_available = await validate_city_geocode(city)
    if not city_ok:
        await safe_send(uid, "❌ Город не найден на карте. Проверьте название и введите ещё раз:")
        return
    data["to_city"] = city
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, OrderStates.trip_date)
    await save_fsm(uid, OrderStates.trip_date, data)
    await safe_send(uid, "📅 Введите дату поездки в формате ДД.ММ.ГГГГ:")

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
        await safe_send(uid, "Создание заказа отменено.", keyboard=kb_main(uid))
        return
    parsed_date = parse_ru_date(message.text)
    if parsed_date is None:
        await safe_send(uid, "❌ Некорректная дата. Введите дату в формате ДД.ММ.ГГГГ (не ранее сегодня и не позже года):")
        return
    data = await _get_fsm_data(uid)
    data["trip_date"] = parsed_date.strftime("%d.%m.%Y")
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, OrderStates.trip_time)
    await save_fsm(uid, OrderStates.trip_time, data)
    await safe_send(uid, "🕐 Введите время поездки в формате ЧЧ:ММ:")

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
        await safe_send(uid, "Создание заказа отменено.", keyboard=kb_main(uid))
        return
    parsed_time = parse_ru_time(message.text)
    if parsed_time is None:
        await safe_send(uid, "❌ Некорректное время. Введите время в формате ЧЧ:ММ (например, 14:30):")
        return
    h, mi = parsed_time
    nice_time = f"{h:02d}:{mi:02d}"
    data = await _get_fsm_data(uid)
    trip_date_str = data.get("trip_date")
    try:
        trip_datetime = datetime.strptime(trip_date_str, "%d.%m.%Y").replace(hour=h, minute=mi, tzinfo=TZ)
        if trip_datetime < datetime.now(TZ):
            await safe_send(uid, "❌ Время поездки не может быть в прошлом. Введите корректное время:")
            return
    except Exception:
        await safe_send(uid, "❌ Ошибка при обработке даты и времени. Введите корректное время:")
        return
    data["trip_time"] = nice_time
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, OrderStates.passengers)
    await save_fsm(uid, OrderStates.passengers, data)
    kb = Keyboard(inline=False)
    kb.add(Text("1"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("2"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("3"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("4"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("5"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("6"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("7"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("8"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    await safe_send(uid, "👥 Шаг 5/6 — Сколько пассажиров?", keyboard=kb.get_json())

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
        await safe_send(uid, "Создание заказа отменено.", keyboard=kb_main(uid))
        return
    try:
        passengers = int(message.text)
        if passengers < 1 or passengers > 8:
            raise ValueError
    except:
        await safe_send(uid, "❌ Введите число от 1 до 8:")
        return
    data = await _get_fsm_data(uid)
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
    await safe_send(uid, f"🚘 Выберите класс автомобиля:{hint}", keyboard=kb.get_json())

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
        await safe_send(uid, "Создание заказа отменено.", keyboard=kb_main(uid))
        return
    class_map = {
        "стандарт": "standard", "комфорт": "comfort",
        "комфорт+": "comfort_plus", "минивэн": "minivan", "бизнес": "business"
    }
    car_class = class_map.get((message.text or "").lower())
    if not car_class:
        await safe_send(uid, "❌ Выберите класс из списка на клавиатуре:")
        return
    data = await _get_fsm_data(uid)
    # Проверяем что класс существует в тарифе нужного региона
    region_tariff = tariffs(data.get("from_city",""), data.get("to_city",""))
    if car_class not in region_tariff:
        await safe_send(uid, "❌ Выберите класс из списка на клавиатуре:")
        return
    # Серверная проверка минивэна для 5+ пассажиров
    if data.get("passengers", 0) >= 5 and car_class != "minivan":
        await safe_send(uid, "❌ Для 5 и более пассажиров доступен только Минивэн. Выберите класс из списка на клавиатуре:")
        return
    data["car_class"] = car_class
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, OrderStates.wishes)
    await save_fsm(uid, OrderStates.wishes, data)
    await safe_send(uid, "💬 Введите дополнительные пожелания (или нажмите 'Пропустить'):", keyboard=kb_skip())

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
        await safe_send(uid, "Создание заказа отменено.", keyboard=kb_main(uid))
        return

    # FIX: используем get(), а не pop() — удаляем только после успешного INSERT
    data = await _get_fsm_data(uid)

    # Fallback: если ключевые поля отсутствуют (сессия устарела в RAM и данные
    # не восстановились из БД полностью), просим начать заново
    required_keys = ('from_city', 'to_city', 'trip_date', 'trip_time', 'passengers', 'car_class')
    if any(k not in data for k in required_keys):
        await bot.state_dispenser.delete(uid)
        fsm_data.pop(uid, None)
        await delete_fsm(uid)
        await safe_send(uid, 
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

    await safe_send(uid, confirm_text, keyboard=kb_main(uid))
    asyncio.create_task(notify_drivers_about_order(order_id))

# ================= РЕГИСТРАЦИЯ ВОДИТЕЛЯ (FSM) =================
@bot.on.message(text="🚗 Я водитель")
async def driver_menu_handler(message: Message):
    uid = message.from_id
    if await check_blacklist(message.from_id):
        await safe_send(uid, "⛔ Вы заблокированы.")
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
        await safe_send(uid, 
            "📱 Для регистрации водителем введите ваш номер телефона в формате +79991234567:",
            keyboard=kb_cancel()
        )
        return

    await delete_fsm(uid)
    fsm_data.pop(uid, None)

    if not is_verified:
        await safe_send(uid, 
            "⏳ Ваш профиль водителя ожидает верификации администратором.",
            keyboard=kb_driver_menu(has_sub)
        )
        return

    if not has_sub:
        await safe_send(uid, 
            "⚠️ У вас нет активной подписки. Приобретите подписку в разделе '💳 Абонемент'.",
            keyboard=kb_driver_menu(has_sub)
        )
        return

    await safe_send(uid, "Выберите действие:", keyboard=kb_driver_menu(has_sub))

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
        await safe_send(uid, "Регистрация отменена.", keyboard=kb_main(uid))
        return
    if not is_valid_phone(message.text):
        await safe_send(uid, "❌ Некорректный номер. Введите в формате +79991234567:")
        return
    fsm_data.setdefault(uid, {})["phone"] = (message.text or "").strip()
    await bot.state_dispenser.set(uid, DriverStates.car_model)
    await save_fsm(uid, DriverStates.car_model, fsm_data[uid])
    await safe_send(uid, "🚗 Введите марку и модель автомобиля:")

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
        await safe_send(uid, "Регистрация отменена.", keyboard=kb_main(uid))
        return
    if len((message.text or "").strip()) < 2:
        await safe_send(uid, "❌ Слишком короткое название. Введите марку и модель:")
        return
    data = await _get_fsm_data(uid)
    data['car_model'] = (message.text or "").strip()
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, DriverStates.car_year)
    await save_fsm(uid, DriverStates.car_year, data)
    await safe_send(uid, "📅 Введите год выпуска автомобиля:")

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
        await safe_send(uid, "Регистрация отменена.", keyboard=kb_main(uid))
        return
    try:
        year = int(message.text)
        if year < 2008 or year > 2030:
            raise ValueError
    except:
        await safe_send(uid, "❌ Введите год от 2008 до 2030:")
        return
    data = await _get_fsm_data(uid)
    data['car_year'] = year
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, DriverStates.car_number)
    await save_fsm(uid, DriverStates.car_number, data)
    await safe_send(uid, "🔢 Введите госномер автомобиля (буквы и цифры, например А123БВ178):")

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
        await safe_send(uid, "Регистрация отменена.", keyboard=kb_main(uid))
        return
    if not is_valid_car_number(message.text):
        await safe_send(uid, "❌ Некорректный номер. Введите буквы и цифры, 4–12 символов:")
        return
    data = await _get_fsm_data(uid)
    data['car_number'] = (message.text or "").strip().upper().replace(" ", "")
    fsm_data[uid] = data
    await bot.state_dispenser.set(uid, DriverStates.car_class)
    await save_fsm(uid, DriverStates.car_class, data)
    # Сброс клавиатуры — воркэраунд бага ВК с пустым отображением кнопок
    await safe_send(uid, "⏳", keyboard='{"buttons":[],"one_time":true}')
    await safe_send(uid, "🚘 Выберите класс вашего автомобиля:", keyboard=kb_car_class_driver())

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
        await safe_send(uid, "Регистрация отменена.", keyboard=kb_main(uid))
        return
    class_map = {
        "стандарт": "standard", "комфорт": "comfort",
        "комфорт+": "comfort_plus", "минивэн": "minivan", "бизнес": "business"
    }
    car_class = class_map.get((message.text or "").lower())
    if not car_class:
        await safe_send(uid, "❌ Выберите класс из списка на клавиатуре:")
        return

    # FIX: get() вместо pop() — удаляем после успешного INSERT
    data = await _get_fsm_data(uid)

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
    await safe_send(uid, 
        f"✅ Регистрация завершена! Ваша заявка отправлена на проверку администратору.{trial_txt}",
        keyboard=kb_driver_menu(False)
    )

# ================= ДОСТУПНЫЕ ЗАКАЗЫ =================
@bot.on.message(text="📦 Доступные заказы")
async def available_orders(message: Message):
    uid = message.from_id
    if await check_blacklist(uid):
        return await safe_send(uid, "⛔ Вы заблокированы.")
    if not await is_driver_registered(uid):
        return await safe_send(uid, "❌ Сначала зарегистрируйтесь как водитель.")
    if not await has_active_sub(uid):
        return await safe_send(uid, "🔒 Нет абонемента.")
    if not await is_driver_verified(uid):
        return await safe_send(uid, "⏳ Профиль ещё не верифицирован.")
    async with _pg_pool.acquire() as conn:
        driver = await conn.fetchrow("SELECT car_class FROM drivers WHERE user_id=$1", uid)
        if not driver:
            return await safe_send(uid, "❌ Профиль водителя не найден.")
        open_orders = await conn.fetch(
            "SELECT * FROM orders WHERE status='open' AND passenger_id != $1 ORDER BY created_at DESC", uid
        )
    if not open_orders:
        return await safe_send(uid, "📭 Нет доступных заказов.")

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
        await safe_send(uid, fmt_order(o), keyboard=kb.get_json())
        await asyncio.sleep(0.05)  # защита от rate limit

    total = len(open_orders)
    summary = f"📦 Открыто: {total} | ✅ Доступно: {can_count}"
    if total > PAGE_SIZE:
        summary += f"\n(показано {PAGE_SIZE} из {total}, остальные появятся по мере выполнения)"
    await safe_send(uid, summary, keyboard=kb_driver_menu(True))

# === Кнопки статуса подписки ===
@bot.on.message(text=["✅ Подписка активна", "❌ Нет подписки"])
async def sub_status_handler(message: Message):
    await subscription_handler(message)

# === Мои заказы ===
@bot.on.message(text="📋 Мои заказы")
async def passenger_orders_list(message: Message):
    uid = message.from_id
    if await check_blacklist(message.from_id):
        await safe_send(uid, "⛔ Вы заблокированы.")
        return
    async with _pg_pool.acquire() as conn:
        orders = await conn.fetch(
            "SELECT * FROM orders WHERE passenger_id=$1 ORDER BY created_at DESC LIMIT 10",
            message.from_id
        )
    if not orders:
        await safe_send(uid, "У вас пока нет заказов.", keyboard=kb_main(message.from_id))
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
        await safe_send(uid, text, keyboard=kb.get_json() if kb else None)
        await asyncio.sleep(0.05)

# === Мои поездки ===
@bot.on.message(text="📈 Мои поездки")
async def driver_trips_list(message: Message):
    uid = message.from_id
    if await check_blacklist(message.from_id):
        await safe_send(uid, "⛔ Вы заблокированы.")
        return
    if not await is_driver_registered(message.from_id):
        await safe_send(uid, "Сначала зарегистрируйтесь как водитель.", keyboard=kb_main(message.from_id))
        return
    async with _pg_pool.acquire() as conn:
        orders = await conn.fetch(
            "SELECT * FROM orders WHERE driver_id=$1 ORDER BY created_at DESC LIMIT 10",
            message.from_id
        )
    if not orders:
        await safe_send(uid, "У вас пока нет поездок.", keyboard=kb_driver_menu(await has_active_sub(message.from_id)))
        return
    for o in orders:
        kb = None
        if o['status'] == 'taken':
            kb = Keyboard(inline=True)
            kb.add(
                Text("❌ Отказаться от заказа", payload={"cmd": "driver_cancel", "order_id": o['id']}),
                color=KeyboardButtonColor.NEGATIVE
            )
        await safe_send(uid, fmt_order(o), keyboard=kb.get_json() if kb else None)

# === Тарифы ===
@bot.on.message(text="📊 Тарифы")
async def show_tariffs(message: Message):
    uid = message.from_id
    if await check_blacklist(message.from_id):
        return await safe_send(uid, "⛔ Вы заблокированы.")
    text = "📊 Тарифы на поездки (₽/км):\n\n"
    text += "🇷🇺 Россия\n"
    text += "  (Вся машина / Место в рейсе)\n"
    for v in TARIFFS_RF.values():
        text += f"  {v['label']} — {v['price']} ₽ / {v['price_seat']} ₽\n"
    text += "\n🌍 Кавказ/СНГ\n"
    text += "  (Вся машина / Место в рейсе)\n"
    for v in TARIFFS_CIS.values():
        text += f"  {v['label']} — {v['price']} ₽ / {v['price_seat']} ₽\n"
    text += "\n🆕 Новые территории\n"
    text += "  (Вся машина / Место в рейсе)\n"
    for v in TARIFFS_NT.values():
        text += f"  {v['label']} — {v['price']} ₽ / {v['price_seat']} ₽\n"
    text += "\n⚠️ Платные дороги оплачиваются отдельно."
    text += "\n🚫 Торг запрещён."
    text += "\n\n📞 Заказ по телефону:\n"
    for phone in DISPATCHER_PHONES:
        text += f"  {phone}\n"
    await safe_send(uid, text, keyboard=kb_main(message.from_id))

# === Абонемент ===
@bot.on.message(text="💳 Абонемент")
async def subscription_handler(message: Message):
    uid = message.from_id
    if await check_blacklist(message.from_id):
        await safe_send(uid, "⛔ Вы заблокированы.")
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
    await safe_send(uid, text, keyboard=kb.get_json())

# === Админ панель ===
@bot.on.message(text="🔧 Админ")
async def admin_panel(message: Message):
    uid = message.from_id
    if message.from_id not in ADMIN_IDS:
        return await safe_send(uid, "⛔ Доступ запрещён.")
    await safe_send(uid, "🔧 Административная панель:", keyboard=kb_admin_menu())

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
        reg = fmt_dt_ru(d.get("registered_at"))
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
    uid = message.from_id
    if message.from_id not in ADMIN_IDS:
        return
    async with _pg_pool.acquire() as conn:
        unverified = await conn.fetch("SELECT * FROM drivers WHERE docs_verified=FALSE LIMIT 10")
    if not unverified:
        await safe_send(uid, "Нет водителей на верификацию.")
        return
    for d in unverified:
        reg_date = fmt_dt_ru(d['registered_at'])
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
        await safe_send(uid, text, keyboard=kb.get_json())

@bot.on.message(text="💳 Подписки")
async def admin_subscriptions(message: Message):
    uid = message.from_id
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
    await safe_send(uid, text)

@bot.on.message(text="📊 Статистика")
async def admin_stats(message: Message):
    uid = message.from_id
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
    await safe_send(uid, text)

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
                return await safe_send(uid, "⛔ Доступ запрещён.")
            target_uid = data.get("user_id")
            if not target_uid:
                log.warning(f"verify_driver: нет user_id в payload: {data!r}")
                return await safe_send(uid, "❌ Ошибка: не найден ID водителя.")
            async with _pg_pool.acquire() as conn:
                await conn.execute("UPDATE drivers SET docs_verified=TRUE WHERE user_id=$1", target_uid)
            await safe_send(target_uid, "✅ Ваш профиль водителя верифицирован! Теперь вы можете принимать заказы.")
            await safe_send(uid, f"✅ Водитель {target_uid} верифицирован.")
            return

        elif cmd == "reject_driver":
            if uid not in ADMIN_IDS:
                return await safe_send(uid, "⛔ Доступ запрещён.")
            target_uid = data.get("user_id")
            if not target_uid:
                return await safe_send(uid, "❌ Ошибка: не найден ID водителя.")
            async with _pg_pool.acquire() as conn:
                await conn.execute("UPDATE drivers SET docs_verified=FALSE WHERE user_id=$1", target_uid)
            await safe_send(target_uid, "❌ Ваша заявка на регистрацию водителя отклонена администратором.")
            await safe_send(uid, f"Заявка водителя {target_uid} отклонена.")
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
                return await safe_send(uid, "❌ Водитель не найден.")
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
            await safe_send(uid, f"✏️ Редактировать: {drv_name_str}\nЧто изменить?", keyboard=kb.get_json())
            return

        elif cmd == "adm_ef":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            field = data.get("field")
            async with _pg_pool.acquire() as conn:
                drv = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", target_uid)
            if not drv:
                return await safe_send(uid, "❌ Водитель не найден.")
            if field == "car_class":
                kb = Keyboard(inline=True)
                for k, lbl in CLASS_LABELS.items():
                    kb.add(Text(lbl, payload={"cmd": "adm_sc", "user_id": target_uid, "car_class": k}), color=KeyboardButtonColor.PRIMARY)
                    kb.row()
                await safe_send(uid, "🏷 Выберите новый класс:", keyboard=kb.get_json())
            else:
                prompts = {"car_model": "🚘 Введите новую марку и модель:", "car_year": "📅 Введите новый год:", "car_number": "🔢 Введите новый гос. номер:"}
                adm_edit_fsm[uid] = {"target_uid": target_uid, "field": field}
                await bot.state_dispenser.set(uid, AdminEditStates.waiting_input)
                await safe_send(uid, prompts.get(field, "Введите значение:"), keyboard=kb_cancel())
            return

        elif cmd == "adm_sc":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            car_class = data.get("car_class")
            async with _pg_pool.acquire() as conn:
                await conn.execute("UPDATE drivers SET car_class=$1 WHERE user_id=$2", car_class, target_uid)
            await safe_send(target_uid, f"🏷 Администратор изменил класс вашего авто на: {CLASS_LABELS.get(car_class, car_class)}")
            await safe_send(uid, f"✅ Класс авто обновлён: {CLASS_LABELS.get(car_class, car_class)}")
            return

        # --- Удаление водителя ---
        elif cmd == "adm_del_drv":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            async with _pg_pool.acquire() as conn:
                drv = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", target_uid)
            if not drv:
                return await safe_send(uid, "❌ Водитель не найден.")
            kb = Keyboard(inline=True)
            kb.add(Text("✅ Да, удалить", payload={"cmd": "adm_del_ok", "user_id": target_uid}), color=KeyboardButtonColor.NEGATIVE)
            kb.add(Text("❌ Нет", payload={"cmd": "adm_del_no"}), color=KeyboardButtonColor.SECONDARY)
            await safe_send(uid, f"⚠️ Удалить водителя id{target_uid}?\n{drv['car_model']} | {drv['phone']}", keyboard=kb.get_json())
            return

        elif cmd == "adm_del_ok":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            async with _pg_pool.acquire() as conn:
                drv = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", target_uid)
                if not drv:
                    active = []
                    active_trip_bookings = []
                else:
                    async with conn.transaction():
                        # Освобождаем активные заказы
                        active = await conn.fetch(
                            "SELECT id, passenger_id FROM orders WHERE driver_id=$1 AND status='taken'", target_uid
                        )
                        for o in active:
                            await conn.execute("UPDATE orders SET status='open', driver_id=NULL WHERE id=$1", o["id"])

                        # Отменяем активные рейсы и их брони
                        trip_rows = await conn.fetch(
                            "SELECT id FROM trips WHERE driver_id=$1 AND status IN ('open','full')", target_uid
                        )
                        trip_ids = [r["id"] for r in trip_rows]
                        active_trip_bookings = []
                        if trip_ids:
                            booking_rows = await conn.fetch(
                                "SELECT trip_id, passenger_id FROM trip_bookings "
                                "WHERE trip_id = ANY($1::int[]) AND status='confirmed'", trip_ids
                            )
                            active_trip_bookings = [(r["trip_id"], r["passenger_id"]) for r in booking_rows]
                            await conn.execute(
                                "UPDATE trip_bookings SET status='cancelled_by_driver' "
                                "WHERE trip_id = ANY($1::int[]) AND status='confirmed'", trip_ids
                            )
                            await conn.execute(
                                "UPDATE trips SET status='cancelled' WHERE driver_id=$1 AND status IN ('open','full')",
                                target_uid
                            )

                        await conn.execute("UPDATE ratings SET driver_id=NULL WHERE driver_id=$1", target_uid)
                        await conn.execute("DELETE FROM subscriptions WHERE user_id=$1", target_uid)
                        await conn.execute("DELETE FROM pending_subscriptions WHERE user_id=$1", target_uid)
                        await conn.execute("DELETE FROM drivers WHERE user_id=$1", target_uid)
            if not drv:
                return await safe_send(uid, "❌ Водитель не найден.")
            # Уведомляем после закрытия соединения
            for o in active:
                if o["passenger_id"]:
                    await safe_send(o["passenger_id"], f"⚠️ Водитель удалён администратором. Заказ #{o['id']} снова открыт.")
            for tid, pid in active_trip_bookings:
                await safe_send(pid, f"⚠️ Водитель удалён администратором. Рейс #{tid} отменён, бронь аннулирована.")
            await safe_send(target_uid, "🗑 Ваш профиль водителя удалён администратором.")
            await safe_send(uid, f"✅ Водитель id{target_uid} удалён.")
            return

        elif cmd == "adm_del_no":
            await safe_send(uid, "❌ Удаление отменено.")
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
                await safe_send(uid, f"ℹ️ id{target_uid} уже в чёрном списке.", keyboard=kb.get_json())
            else:
                async with _pg_pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO blacklist(user_id, reason) VALUES($1,$2) ON CONFLICT DO NOTHING",
                        target_uid, "Заблокирован администратором"
                    )
                await safe_send(target_uid, "🚫 Вы заблокированы администратором.")
                await safe_send(uid, f"🚫 id{target_uid} добавлен в чёрный список.")
            return

        elif cmd == "adm_unban_drv":
            if uid not in ADMIN_IDS:
                return
            target_uid = data.get("user_id")
            async with _pg_pool.acquire() as conn:
                await conn.execute("DELETE FROM blacklist WHERE user_id=$1", target_uid)
            await safe_send(target_uid, "✅ Вы разблокированы администратором.")
            await safe_send(uid, f"✅ id{target_uid} удалён из чёрного списка.")
            return

        elif cmd == "activate_sub":
            if uid not in ADMIN_IDS:
                return await safe_send(uid, "⛔ Доступ запрещён.")
            target_uid = data.get("user_id")
            plan_key = data.get("plan_key")
            plan = SUBS.get(plan_key)
            if not plan or not target_uid:
                return await safe_send(uid, "❌ Ошибка данных.")
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
            await safe_send(uid, f"✅ Подписка для {target_uid} активирована до {new_exp.strftime('%d.%m.%Y')}")
            return

        elif cmd == "reject_sub":
            if uid not in ADMIN_IDS:
                return await safe_send(uid, "⛔ Доступ запрещён.")
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
            await safe_send(uid, f"Заявка на подписку от {target_uid} отклонена.")
            return

        elif cmd == "adm_cancel_order":
            if uid not in ADMIN_IDS:
                return await safe_send(uid, "⛔ Доступ запрещён.")
            order_id = data.get("order_id")
            if not order_id:
                return
            order = await get_order(order_id)
            if not order:
                return await safe_send(uid, "Заказ не найден.")
            await update_order_status(order_id, "cancelled")
            await safe_send(uid, f"❌ Заказ #{order_id} отменён.")
            if order['passenger_id']:
                await safe_send(order['passenger_id'], f"❌ Ваш заказ #{order_id} отменён администратором.")
            if order['driver_id']:
                await safe_send(order['driver_id'], f"❌ Заказ #{order_id} отменён администратором.")
            return

        elif cmd == "adm_complete_order":
            if uid not in ADMIN_IDS:
                return await safe_send(uid, "⛔ Доступ запрещён.")
            order_id = data.get("order_id")
            if not order_id:
                return
            order = await get_order(order_id)
            if not order:
                return await safe_send(uid, "Заказ не найден.")
            await update_order_status(order_id, "completed", driver_id=order['driver_id'])
            await safe_send(uid, f"✅ Заказ #{order_id} завершён.")
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
            await safe_send(uid, "⛔ Вы заблокированы.")
            return

        if cmd == "take_order":
            order_id = data.get("order_id")
            if not await is_driver_verified(uid):
                return await safe_send(uid, "❌ Ваш профиль не верифицирован.")
            if not await has_active_sub(uid):
                return await safe_send(uid, "❌ Нет активной подписки.")
            async with _pg_pool.acquire() as conn:
                driver_data = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", uid)
            if not driver_data:
                return await safe_send(uid, "❌ Профиль водителя не найден.")
            success, error_msg, order = await try_take_order_atomic(order_id, uid, driver_data['car_class'])
            if not success:
                return await safe_send(uid, f"❌ {error_msg}")
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
            await safe_send(uid, 
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
            await safe_send(uid, "Вы пропустили этот заказ.")
            return

        elif cmd == "done_order":
            order_id = data.get("order_id")
            order = await get_order(order_id)
            if not order or order['passenger_id'] != uid:
                return await safe_send(uid, "❌ Ошибка: этот заказ не ваш.")
            if order['status'] != 'taken':
                return await safe_send(uid, "❌ Заказ не в работе.")
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
            await safe_send(uid, 
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
                return await safe_send(uid, "❌ Ошибка: этот заказ не ваш.")
            if order['status'] not in ['open', 'taken']:
                return await safe_send(uid, "❌ Заказ нельзя отменить.")
            await update_order_status(order_id, 'cancelled')
            await safe_send(uid, f"❌ Заказ #{order_id} отменён.")
            if order['driver_id']:
                await safe_send(order['driver_id'], f"❌ Пассажир отменил заказ #{order_id}.")
            return

        elif cmd == "driver_cancel":
            order_id = data.get("order_id")
            order = await get_order(order_id)
            if not order or order['driver_id'] != uid:
                return await safe_send(uid, "❌ Вы не водитель этого заказа.")
            if order['status'] != 'taken':
                return await safe_send(uid, "❌ Заказ нельзя отменить.")
            await update_order_status(order_id, 'open', clear_driver=True)
            await safe_send(uid, f"Вы отказались от заказа #{order_id}.")
            await safe_send(order['passenger_id'], f"⚠️ Водитель отказался от заказа #{order_id}.")
            asyncio.create_task(notify_drivers_about_order(order_id, exclude_drivers=[uid]))
            return

        elif cmd == "rate_order":
            order_id = data.get("order_id")
            stars = int(data.get("stars", 0))
            order = await get_order(order_id)
            if not order or order['passenger_id'] != uid:
                return await safe_send(uid, "❌ Ошибка: этот заказ не ваш.")
            if order['status'] != 'completed':
                return await safe_send(uid, "❌ Поездка ещё не завершена.")
            if stars < 1 or stars > 5:
                return await safe_send(uid, "❌ Оценка должна быть от 1 до 5.")
            # Проверка повторной оценки
            if await has_rating(order_id, uid):
                return await safe_send(uid, "Вы уже оценили эту поездку.")
            drv_id = order['driver_id']
            if not drv_id:
                return await safe_send(uid, "❌ Водитель не найден.")
            await add_rating(order_id, drv_id, uid, stars)
            avg, cnt = await avg_rating(drv_id)
            stars_display = "⭐" * stars + "☆" * (5 - stars)
            await safe_send(uid, 
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
                return await safe_send(uid, "❌ Сначала зарегистрируйтесь как водитель.")
            plan_key = data.get("plan_key")
            plan = SUBS.get(plan_key)
            if not plan:
                return await safe_send(uid, "❌ Ошибка тарифного плана.")
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
            await safe_send(uid, f"✅ Заявка на подписку '{plan['label']}' отправлена администратору.")
            return

        elif cmd == "book_trip":
            trip_id = data.get("trip_id")
            if not trip_id:
                return await safe_send(uid, "❌ Ошибка: ID рейса не найден.")
            async with _pg_pool.acquire() as conn:
                trip = await conn.fetchrow("SELECT * FROM trips WHERE id=$1", trip_id)
            if not trip:
                return await safe_send(uid, "❌ Рейс не найден.")
            if trip["status"] not in ("open","full"):
                return await safe_send(uid, "❌ Рейс недоступен для бронирования.")
            if trip["seats_free"] <= 0:
                return await safe_send(uid, "❌ Мест нет — все места уже заняты.")
            if trip["driver_id"] == uid:
                return await safe_send(uid, "❌ Нельзя бронировать собственный рейс.")
            max_seats = min(trip["seats_free"], 4)
            kb = Keyboard(inline=True)
            for i in range(1, max_seats + 1):
                kb.add(Text(str(i), payload={"cmd": "book_trip_confirm", "trip_id": trip_id, "seats": i}),
                       color=KeyboardButtonColor.PRIMARY)
                if i % 4 == 0:
                    kb.row()
            kb.row()
            kb.add(Text("❌ Отменить", payload={"cmd": "book_seats_cancel"}), color=KeyboardButtonColor.NEGATIVE)
            return await safe_send(uid, f"👥 Сколько мест забронировать? (свободно: {trip['seats_free']})",
                                   keyboard=kb.get_json())

        elif cmd == "book_seats_cancel":
            return await safe_send(uid, "Отменено.", keyboard=kb_main(uid))

        elif cmd == "book_trip_confirm":
            trip_id = data.get("trip_id")
            seats = int(data.get("seats", 1))
            if not trip_id:
                return await safe_send(uid, "❌ Ошибка: ID рейса не найден.")
            async with _pg_pool.acquire() as conn:
                trip = await conn.fetchrow("SELECT * FROM trips WHERE id=$1", trip_id)
            if not trip:
                return await safe_send(uid, "❌ Рейс не найден.")
            if trip["status"] not in ("open","full"):
                return await safe_send(uid, "❌ Рейс недоступен для бронирования.")
            if trip["seats_free"] < seats:
                return await safe_send(uid, "❌ Столько мест уже не осталось.")
            if trip["driver_id"] == uid:
                return await safe_send(uid, "❌ Нельзя бронировать собственный рейс.")
            async with _pg_pool.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT 1 FROM trip_bookings WHERE trip_id=$1 AND passenger_id=$2 AND status='confirmed'",
                    trip_id, uid
                )
            if existing:
                return await safe_send(uid, "❌ Вы уже забронировали место на этот рейс.")
            # Атомарное бронирование
            async with _pg_pool.acquire() as conn:
                async with conn.transaction():
                    result = await conn.execute(
                        "UPDATE trips SET seats_free=seats_free-$2 WHERE id=$1 AND seats_free>=$2 AND status='open'",
                        trip_id, seats
                    )
                    if int(result.split()[-1]) == 0:
                        return await safe_send(uid, "❌ Не удалось забронировать — место(-а) только что заняли.")
                    await conn.execute(
                        "INSERT INTO trip_bookings(trip_id,passenger_id,seats,status,created_at) "
                        "VALUES($1,$2,$3,'confirmed',NOW()) "
                        "ON CONFLICT (trip_id,passenger_id) DO UPDATE SET "
                        "status='confirmed', seats=EXCLUDED.seats, created_at=NOW()",
                        trip_id, uid, seats
                    )
                    await conn.execute("UPDATE trips SET status='full' WHERE id=$1 AND seats_free=0", trip_id)
                    trip = await conn.fetchrow("SELECT * FROM trips WHERE id=$1", trip_id)
            drv_id = trip["driver_id"]
            trip_date_str = trip['trip_date'].strftime('%d.%m.%Y') if hasattr(trip['trip_date'],'strftime') else str(trip['trip_date'])
            trip_time_str = trip['trip_time'].strftime('%H:%M') if hasattr(trip['trip_time'],'strftime') else str(trip['trip_time'])
            async with _pg_pool.acquire() as conn:
                drv_data = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", drv_id)
            maps_url = yandex_maps_url(trip['from_city'], trip['to_city'])
            seats_left = trip["seats_free"]
            is_full = trip["status"] == "full"

            # ── Клавиатура пассажира ──
            kb_pass = Keyboard(inline=True)
            kb_pass.add(OpenLink(label="📞 Написать водителю", link=f"https://vk.com/id{drv_id}"))
            kb_pass.row()
            kb_pass.add(OpenLink(label="🗺 Маршрут", link=maps_url))
            kb_pass.row()
            kb_pass.add(
                Text("ℹ️ Инфо о рейсе", payload={"cmd":"trip_info","trip_id":trip_id}),
                color=KeyboardButtonColor.SECONDARY
            )
            kb_pass.add(
                Text("👥 Пассажиры рейса", payload={"cmd":"trip_passengers_public","trip_id":trip_id}),
                color=KeyboardButtonColor.SECONDARY
            )
            kb_pass.row()
            kb_pass.add(
                Text("❌ Отменить бронь", payload={"cmd":"cancel_booking","trip_id":trip_id}),
                color=KeyboardButtonColor.NEGATIVE
            )

            await safe_send(uid, 
                f"🎉 Место забронировано!\n\n"
                f"🚐 Рейс #{trip_id}\n"
                f"📍 {esc(trip['from_city'])} → {esc(trip['to_city'])}\n"
                f"📅 {trip_date_str} · {trip_time_str}\n"
                f"🚘 {esc(trip['car_class_label'])}\n"
                f"👤 Водитель: {vk_link(drv_id)}\n"
                f"📞 {esc(drv_data['phone']) if drv_data else '—'}\n"
                f"💰 К оплате: {trip['price_per_seat']} ₽\n\n"
                f"🚫 Не переводите предоплату! Оплата — только водителю после поездки.",
                keyboard=kb_pass.get_json()
            )

            # ── Имя пассажира для водителя ──
            try:
                vk_u = await bot.api.users.get(user_ids=[uid])
                pass_name = f"{vk_u[0].first_name} {vk_u[0].last_name}" if vk_u else f"id{uid}"
            except:
                pass_name = f"id{uid}"

            if is_full:
                # Рейс полностью набран — отдельное уведомление водителю
                kb_full = Keyboard(inline=True)
                kb_full.add(
                    Text("👥 Все пассажиры", payload={"cmd":"trip_passengers","trip_id":trip_id}),
                    color=KeyboardButtonColor.PRIMARY
                )
                kb_full.row()
                kb_full.add(OpenLink(label="🗺 Маршрут", link=maps_url))
                kb_full.row()
                kb_full.add(
                    Text("✅ Завершить рейс", payload={"cmd":"trip_complete","trip_id":trip_id}),
                    color=KeyboardButtonColor.POSITIVE
                )
                kb_full.row()
                kb_full.add(
                    Text("❌ Отменить рейс", payload={"cmd":"trip_cancel","trip_id":trip_id}),
                    color=KeyboardButtonColor.NEGATIVE
                )
                await safe_send(
                    drv_id,
                    f"🔵 Рейс #{trip_id} полностью набран!\n"
                    f"📍 {esc(trip['from_city'])} → {esc(trip['to_city'])}\n"
                    f"📅 {trip_date_str} · {trip_time_str}\n"
                    f"💺 Все {trip['seats_total']} мест заняты\n"
                    f"💰 Итого: {trip['price_per_seat'] * trip['seats_total']} ₽",
                    keyboard=kb_full.get_json()
                )
            else:
                # Обычное уведомление о новом пассажире
                kb_drv = Keyboard(inline=True)
                kb_drv.add(OpenLink(label="💬 Написать пассажиру", link=f"https://vk.com/id{uid}"))
                kb_drv.row()
                kb_drv.add(
                    Text("👥 Все пассажиры", payload={"cmd":"trip_passengers","trip_id":trip_id}),
                    color=KeyboardButtonColor.SECONDARY
                )
                kb_drv.add(OpenLink(label="🗺 Маршрут", link=maps_url))
                kb_drv.row()
                kb_drv.add(
                    Text("✅ Завершить рейс", payload={"cmd":"trip_complete","trip_id":trip_id}),
                    color=KeyboardButtonColor.POSITIVE
                )
                kb_drv.row()
                kb_drv.add(
                    Text("❌ Отклонить пассажира", payload={"cmd":"trip_reject_passenger","trip_id":trip_id,"passenger_id":uid}),
                    color=KeyboardButtonColor.NEGATIVE
                )
                await safe_send(
                    drv_id,
                    f"🔔 Новая бронь на рейс #{trip_id}!\n"
                    f"👤 {vk_link(uid, pass_name)}\n"
                    f"💺 Занято: {trip['seats_total']-seats_left}/{trip['seats_total']} · осталось: {seats_left}\n"
                    f"💰 Оплатит: {trip['price_per_seat']} ₽",
                    keyboard=kb_drv.get_json()
                )
            return

        elif cmd == "cancel_booking":
            trip_id = data.get("trip_id")
            if not trip_id:
                return await safe_send(uid, "❌ Ошибка: ID рейса не найден.")
            async with _pg_pool.acquire() as conn:
                trip = await conn.fetchrow("SELECT * FROM trips WHERE id=$1", trip_id)
                booking = await conn.fetchrow(
                    "SELECT seats FROM trip_bookings WHERE trip_id=$1 AND passenger_id=$2 AND status='confirmed'",
                    trip_id, uid
                )
            if not booking:
                return await safe_send(uid, "❌ Бронь не найдена или уже отменена.")
            async with _pg_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE trip_bookings SET status='cancelled_by_passenger' WHERE trip_id=$1 AND passenger_id=$2",
                        trip_id, uid
                    )
                    await conn.execute(
                        "UPDATE trips SET seats_free=seats_free+$1, "
                        "status=CASE WHEN status='full' THEN 'open' ELSE status END WHERE id=$2",
                        booking["seats"], trip_id
                    )
            await safe_send(uid, 
                f"✅ Бронь на рейс #{trip_id} отменена.\n"
                f"📍 {esc(trip['from_city']) if trip else '—'} → {esc(trip['to_city']) if trip else '—'}",
                keyboard=kb_main(uid)
            )
            if trip:
                await safe_send(trip["driver_id"], f"ℹ️ Пассажир {vk_link(uid)} отменил бронь на рейс #{trip_id}. Место освободилось.")
            return

        elif cmd == "trip_cancel":
            trip_id = data.get("trip_id")
            if not trip_id:
                return await safe_send(uid, "❌ Ошибка.")
            async with _pg_pool.acquire() as conn:
                trip = await conn.fetchrow("SELECT * FROM trips WHERE id=$1 AND driver_id=$2", trip_id, uid)
            if not trip:
                return await safe_send(uid, "❌ Рейс не найден.")
            if trip["status"] not in ("open","full"):
                return await safe_send(uid, "❌ Рейс уже не активен.")
            async with _pg_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("UPDATE trips SET status='cancelled' WHERE id=$1", trip_id)
                    passengers = await conn.fetch(
                        "SELECT passenger_id FROM trip_bookings WHERE trip_id=$1 AND status='confirmed'", trip_id
                    )
                    await conn.execute(
                        "UPDATE trip_bookings SET status='cancelled_by_driver' WHERE trip_id=$1 AND status='confirmed'", trip_id
                    )
            await safe_send(uid, f"✅ Рейс #{trip_id} отменён.")
            trip_date_str = trip['trip_date'].strftime('%d.%m.%Y') if hasattr(trip['trip_date'],'strftime') else str(trip['trip_date'])
            for p in passengers:
                await safe_send(
                    p["passenger_id"],
                    f"❌ Рейс #{trip_id} отменён водителем.\n"
                    f"📍 {esc(trip['from_city'])} → {esc(trip['to_city'])} · {trip_date_str}\n"
                    "Попробуйте найти другой рейс: 🔍 Найти место в рейсе"
                )
            return

        elif cmd == "trip_info":
            # Пассажир запрашивает актуальную карточку рейса
            trip_id = data.get("trip_id")
            if not trip_id:
                return await safe_send(uid, "❌ Ошибка.")
            async with _pg_pool.acquire() as conn:
                trip = await conn.fetchrow("SELECT * FROM trips WHERE id=$1", trip_id)
            if not trip:
                return await safe_send(uid, "❌ Рейс не найден.")
            maps_url = yandex_maps_url(trip['from_city'], trip['to_city'])
            kb_i = Keyboard(inline=True)
            kb_i.add(OpenLink(label="📞 Написать водителю", link=f"https://vk.com/id{trip['driver_id']}"))
            kb_i.row()
            kb_i.add(OpenLink(label="🗺 Маршрут", link=maps_url))
            kb_i.row()
            kb_i.add(
                Text("👥 Пассажиры рейса", payload={"cmd":"trip_passengers_public","trip_id":trip_id}),
                color=KeyboardButtonColor.SECONDARY
            )
            kb_i.row()
            kb_i.add(
                Text("❌ Отменить бронь", payload={"cmd":"cancel_booking","trip_id":trip_id}),
                color=KeyboardButtonColor.NEGATIVE
            )
            return await safe_send(uid, fmt_trip(trip), keyboard=kb_i.get_json())

        elif cmd == "trip_passengers_public":
            # Пассажир видит список попутчиков (без кнопки отклонить)
            trip_id = data.get("trip_id")
            if not trip_id:
                return await safe_send(uid, "❌ Ошибка.")
            async with _pg_pool.acquire() as conn:
                trip = await conn.fetchrow("SELECT * FROM trips WHERE id=$1", trip_id)
                bookings = await conn.fetch(
                    "SELECT * FROM trip_bookings WHERE trip_id=$1 AND status='confirmed'", trip_id
                )
            if not bookings:
                return await safe_send(uid, "👥 Пока только вы.")
            # Батч-запрос всех имён за один вызов API
            pids = [b["passenger_id"] for b in bookings]
            try:
                vk_users = await bot.api.users.get(user_ids=pids)
                names_dict = {u.id: f"{u.first_name} {u.last_name}" for u in vk_users}
            except Exception:
                names_dict = {}
            lines = [f"👥 Пассажиры рейса #{trip_id} ({len(bookings)} чел.):"]
            for b in bookings:
                pid = b["passenger_id"]
                pname = names_dict.get(pid, f"id{pid}")
                marker = " ← вы" if pid == uid else ""
                lines.append(f"  · {vk_link(pid, pname)}{marker}")
            return await safe_send(uid, "\n".join(lines))

        elif cmd == "trip_complete":
            # Водитель завершает рейс
            trip_id = data.get("trip_id")
            if not trip_id:
                return await safe_send(uid, "❌ Ошибка.")
            async with _pg_pool.acquire() as conn:
                trip = await conn.fetchrow("SELECT * FROM trips WHERE id=$1 AND driver_id=$2", trip_id, uid)
            if not trip:
                return await safe_send(uid, "❌ Рейс не найден.")
            if trip["status"] not in ("open","full"):
                return await safe_send(uid, "❌ Рейс уже завершён или отменён.")
            async with _pg_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("UPDATE trips SET status='done' WHERE id=$1", trip_id)
                    passengers = await conn.fetch(
                        "SELECT passenger_id FROM trip_bookings WHERE trip_id=$1 AND status='confirmed'", trip_id
                    )
            await safe_send(uid, f"✅ Рейс #{trip_id} завершён!")
            trip_date_str = trip['trip_date'].strftime('%d.%m.%Y') if hasattr(trip['trip_date'],'strftime') else str(trip['trip_date'])
            # Уведомляем каждого пассажира с предложением поставить оценку
            for p in passengers:
                pid = p["passenger_id"]
                kb_rate = Keyboard(inline=True)
                for stars in range(1, 6):
                    kb_rate.add(
                        Text(f"{stars}⭐", payload={"cmd":"rate_driver","trip_id":trip_id,"driver_id":uid,"stars":stars}),
                        color=KeyboardButtonColor.PRIMARY
                    )
                await safe_send(
                    pid,
                    f"✅ Рейс #{trip_id} завершён водителем.\n"
                    f"📍 {esc(trip['from_city'])} → {esc(trip['to_city'])} · {trip_date_str}\n\n"
                    f"⭐ Пожалуйста, оцените водителя:",
                    keyboard=kb_rate.get_json()
                )
            return

        elif cmd == "rate_driver":
            # Пассажир ставит оценку водителю за рейс
            trip_id   = data.get("trip_id")
            driver_id = data.get("driver_id")
            stars     = data.get("stars")
            if not trip_id or not driver_id or not stars:
                return await safe_send(uid, "❌ Ошибка данных.")
            # Используем trip_id как order_id в таблице ratings со знаком минус,
            # чтобы не конфликтовать с обычными заказами (id заказов всегда положительные)
            rating_key = -trip_id
            # Оценить может только тот, кто реально бронировал этот рейс —
            # иначе кто угодно, подобрав payload, мог бы накрутить рейтинг.
            async with _pg_pool.acquire() as conn:
                was_passenger = await conn.fetchval(
                    "SELECT 1 FROM trip_bookings WHERE trip_id=$1 AND passenger_id=$2", trip_id, uid)
            if not was_passenger:
                return await safe_send(uid, "❌ Вы не бронировали этот рейс.")
            # Проверяем — не оценивал ли уже
            if await has_rating(rating_key, uid):
                return await safe_send(uid, "Вы уже оценили эту поездку.")
            await add_rating(rating_key, driver_id, uid, stars)
            avg, cnt = await avg_rating(driver_id)
            stars_display = "⭐" * stars + "☆" * (5 - stars)
            await safe_send(uid, 
                f"{stars_display} Спасибо за оценку!\n"
                f"Средний рейтинг водителя: {avg}/5 ({cnt} оценок)"
            )
            await safe_send(
                driver_id,
                f"⭐ Новая оценка за рейс #{trip_id}: {stars_display} ({stars}/5)\n"
                f"Ваш средний рейтинг: {avg}/5 ({cnt} оценок)"
            )
            return

        elif cmd == "trip_passengers":
            trip_id = data.get("trip_id")
            if not trip_id:
                return await safe_send(uid, "❌ Ошибка.")
            async with _pg_pool.acquire() as conn:
                trip = await conn.fetchrow("SELECT * FROM trips WHERE id=$1 AND driver_id=$2", trip_id, uid)
            if not trip:
                return await safe_send(uid, "❌ Рейс не найден.")
            async with _pg_pool.acquire() as conn:
                bookings = await conn.fetch(
                    "SELECT * FROM trip_bookings WHERE trip_id=$1 AND status='confirmed'", trip_id
                )
            if not bookings:
                return await safe_send(uid, "👥 Пассажиров пока нет.")
            maps_url = yandex_maps_url(trip['from_city'], trip['to_city'])
            await safe_send(uid,
                f"👥 Пассажиры рейса #{trip_id} ({len(bookings)}/{trip['seats_total']}):"
            )
            # Батч-запрос всех имён за один вызов API
            pids = [b["passenger_id"] for b in bookings]
            try:
                vk_users = await bot.api.users.get(user_ids=pids)
                names_dict = {u.id: f"{u.first_name} {u.last_name}" for u in vk_users}
            except Exception:
                names_dict = {}
            for b in bookings:
                pid = b["passenger_id"]
                pname = names_dict.get(pid, f"id{pid}")
                book_date = fmt_dt_ru(b.get("created_at"), with_time=True)
                kb_p = Keyboard(inline=True)
                kb_p.add(OpenLink(label="💬 Написать", link=f"https://vk.com/id{pid}"))
                kb_p.row()
                kb_p.add(
                    Text("❌ Отклонить", payload={"cmd":"trip_reject_passenger","trip_id":trip_id,"passenger_id":pid}),
                    color=KeyboardButtonColor.NEGATIVE
                )
                await safe_send(uid,
                    f"👤 {vk_link(pid, pname)}\n💺 Мест: {b['seats']}\n🕐 Забронировал: {book_date}",
                    keyboard=kb_p.get_json()
                )
                await asyncio.sleep(0.05)
            # После списка — общие кнопки рейса
            kb_trip_mgmt = Keyboard(inline=True)
            kb_trip_mgmt.add(OpenLink(label="🗺 Маршрут", link=maps_url))
            kb_trip_mgmt.row()
            kb_trip_mgmt.add(
                Text("✅ Завершить рейс", payload={"cmd":"trip_complete","trip_id":trip_id}),
                color=KeyboardButtonColor.POSITIVE
            )
            kb_trip_mgmt.row()
            kb_trip_mgmt.add(
                Text("❌ Отменить рейс", payload={"cmd":"trip_cancel","trip_id":trip_id}),
                color=KeyboardButtonColor.NEGATIVE
            )
            await safe_send(uid, "Управление рейсом:", keyboard=kb_trip_mgmt.get_json())
            return

        elif cmd == "trip_reject_passenger":
            trip_id = data.get("trip_id")
            passenger_id = data.get("passenger_id")
            if not trip_id or not passenger_id:
                return await safe_send(uid, "❌ Ошибка данных.")
            async with _pg_pool.acquire() as conn:
                trip = await conn.fetchrow("SELECT * FROM trips WHERE id=$1 AND driver_id=$2", trip_id, uid)
                booking = await conn.fetchrow(
                    "SELECT seats FROM trip_bookings WHERE trip_id=$1 AND passenger_id=$2 AND status='confirmed'",
                    trip_id, passenger_id
                )
            if not trip:
                return await safe_send(uid, "❌ Рейс не найден.")
            if not booking:
                return await safe_send(uid, "❌ Бронь не найдена или уже отменена.")
            async with _pg_pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE trip_bookings SET status='cancelled_by_driver' WHERE trip_id=$1 AND passenger_id=$2",
                        trip_id, passenger_id
                    )
                    await conn.execute(
                        "UPDATE trips SET seats_free=seats_free+$1, "
                        "status=CASE WHEN status='full' THEN 'open' ELSE status END WHERE id=$2",
                        booking["seats"], trip_id
                    )
            await safe_send(uid, f"✅ Пассажир {vk_link(passenger_id)} отклонён. Место возвращено.")
            await safe_send(
                passenger_id,
                f"❌ Водитель отклонил вашу бронь на рейс #{trip_id}.\n"
                f"📍 {esc(trip['from_city'])} → {esc(trip['to_city'])}\n"
                "Попробуйте другой рейс: 🔍 Найти место в рейсе"
            )
            return

    except Exception as e:
        log.error(f"Ошибка в unified_payload_handler: {e}", exc_info=True)
        await safe_send(uid, "❌ Произошла ошибка. Попробуйте позже.")

# === Редактирование водителя администратором (ввод нового значения) ===
@bot.on.message(state=AdminEditStates.waiting_input)
async def adm_edit_input(message: Message):
    uid = message.from_id
    if uid not in ADMIN_IDS:
        return
    edit_data = adm_edit_fsm.pop(uid, None)
    if not edit_data:
        await bot.state_dispenser.delete(uid)
        return await safe_send(uid, "❌ Сессия редактирования истекла.")
    target_uid = edit_data["target_uid"]
    field = edit_data["field"]
    text = (message.text or "").strip()
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(uid)
        return await safe_send(uid, "❌ Редактирование отменено.", keyboard=kb_admin_menu())
    async with _pg_pool.acquire() as conn:
        if field == "car_model":
            if len(text) < 2 or len(text) > 100:
                adm_edit_fsm[uid] = edit_data
                return await safe_send(uid, "❌ Введите марку и модель (2–100 символов):")
            await conn.execute("UPDATE drivers SET car_model=$1 WHERE user_id=$2", text, target_uid)
            await safe_send(target_uid, f"🚘 Администратор изменил марку/модель вашего авто: {text}")
        elif field == "car_year":
            try:
                year = int(text)
                if year < 2008 or year > 2030:
                    adm_edit_fsm[uid] = edit_data
                    return await safe_send(uid, "❌ Год должен быть от 2008 до 2030:")
                await conn.execute("UPDATE drivers SET car_year=$1 WHERE user_id=$2", year, target_uid)
                await safe_send(target_uid, f"📅 Администратор изменил год выпуска вашего авто: {year}")
            except ValueError:
                adm_edit_fsm[uid] = edit_data
                return await safe_send(uid, "❌ Введите корректный год:")
        elif field == "car_number":
            number = text.upper().replace(" ", "")
            if not number:
                adm_edit_fsm[uid] = edit_data
                return await safe_send(uid, "❌ Введите номер:")
            await conn.execute("UPDATE drivers SET car_number=$1 WHERE user_id=$2", number, target_uid)
            await safe_send(target_uid, f"🔢 Администратор изменил гос. номер вашего авто: {number}")
    await bot.state_dispenser.delete(uid)
    await safe_send(uid, f"✅ Поле обновлено: {esc(text)}", keyboard=kb_admin_menu())

# === Команды бана/разбана ===
@bot.on.message(func=lambda m: m.text and m.text.startswith("/ban "))
async def admin_ban(message: Message):
    uid = message.from_id
    if message.from_id not in ADMIN_IDS:
        return await safe_send(uid, "⛔ Доступ запрещён.")
    parts = (message.text or "").strip().split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await safe_send(uid, "Использование: /ban ID [причина]")
        return
    target_id = int(parts[1])
    reason = parts[2] if len(parts) > 2 else "Не указана"
    async with _pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO blacklist(user_id, reason) VALUES($1,$2) ON CONFLICT DO NOTHING",
            target_id, reason
        )
    await safe_send(uid, f"🚫 Пользователь {target_id} заблокирован.")
    await safe_send(target_id, f"⛔ Вы были заблокированы администратором.\nПричина: {reason}")

@bot.on.message(func=lambda m: m.text and m.text.startswith("/unban "))
async def admin_unban(message: Message):
    uid = message.from_id
    if message.from_id not in ADMIN_IDS:
        return await safe_send(uid, "⛔ Доступ запрещён.")
    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await safe_send(uid, "Использование: /unban ID")
        return
    target_id = int(parts[1])
    async with _pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM blacklist WHERE user_id=$1", target_id)
    await safe_send(uid, f"✅ Пользователь {target_id} разблокирован.")
    await safe_send(target_id, "✅ Ваша блокировка снята администратором.")

# === Профиль водителя ===
@bot.on.message(text="👤 Мой профиль")
async def driver_profile(message: Message):
    uid = message.from_id
    if await check_blacklist(message.from_id):
        return await safe_send(uid, "⛔ Вы заблокированы.")
    if not await is_driver_registered(message.from_id):
        return await safe_send(uid, "Вы не зарегистрированы как водитель.", keyboard=kb_main(message.from_id))
    async with _pg_pool.acquire() as conn:
        driver = await conn.fetchrow("SELECT * FROM drivers WHERE user_id=$1", message.from_id)
    if not driver:
        return await safe_send(uid, "Профиль не найден.", keyboard=kb_main(message.from_id))
    has_sub = await has_active_sub(message.from_id)
    avg, cnt = await avg_rating(message.from_id)
    reg_date = fmt_dt_ru(driver['registered_at'])
    text = (
        f"👤 Профиль водителя:\n\n"
        f"📱 Телефон: {driver['phone']}\n"
        f"🚗 Автомобиль: {driver['car_model']} ({driver['car_year']})\n"
        f"🔢 Номер: {driver['car_number']}\n"
        f"🚘 Класс: {CLASS_LABELS.get(driver['car_class'], driver['car_class'])}\n"
        f"✅ Верификация: {'Да' if driver['docs_verified'] else 'Нет'}\n"
        f"💳 Подписка: {'Активна' if has_sub else 'Отсутствует'}\n"
        f"⭐ Рейтинг: {avg}/5 ({cnt} оценок)\n"
        f"📅 Зарегистрирован: {reg_date}"
    )
    await safe_send(uid, text, keyboard=kb_driver_menu(has_sub))

# ================= РЕЙСЫ — ВОДИТЕЛЬ СОЗДАЁТ РЕЙС =================

async def _fsm_clear(uid: int):
    """Полная очистка FSM: RAM + state_dispenser + БД."""
    fsm_data.pop(uid, None)
    fsm_data_ts.pop(uid, None)
    try:
        await bot.state_dispenser.delete(uid)
    except KeyError:
        pass
    await delete_fsm(uid)

@bot.on.message(text="🚐 Создать рейс")
async def trip_create_start(message: Message):
    uid = message.from_id
    if await check_blacklist(uid):
        return await safe_send(uid, "⛔ Вы заблокированы.")
    if not await is_driver_verified(uid):
        return await safe_send(uid, "❌ Профиль не верифицирован администратором.")
    if not await has_active_sub(uid):
        return await safe_send(uid, "❌ Нет активного абонемента.")
    await bot.state_dispenser.set(uid, TripStates.from_city)
    fsm_data[uid] = {}
    fsm_data_ts[uid] = asyncio.get_event_loop().time()
    await save_fsm(uid, TripStates.from_city, {})
    await safe_send(uid, "🚐 Создание рейса\n\n📍 Шаг 1/6 — Откуда?", keyboard=kb_cancel())

@bot.on.message(state=TripStates.from_city)
async def trip_from_city(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        await _fsm_clear(uid)
        return await safe_send(uid, "❌ Создание рейса отменено.", keyboard=kb_driver_menu(await has_active_sub(uid)))
    if not is_valid_city(message.text):
        return await safe_send(uid, "❌ Некорректное название города. Введите ещё раз:")
    data = {"from_city": (message.text or "").strip()}
    fsm_data[uid] = data
    fsm_data_ts[uid] = asyncio.get_event_loop().time()
    await bot.state_dispenser.set(uid, TripStates.to_city)
    await save_fsm(uid, TripStates.to_city, data)
    await safe_send(uid, f"✅ Откуда: {data['from_city']}\n\n🏙 Шаг 2/6 — Куда?")

@bot.on.message(state=TripStates.to_city)
async def trip_to_city(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        await _fsm_clear(uid)
        return await safe_send(uid, "❌ Отменено.", keyboard=kb_driver_menu(await has_active_sub(uid)))
    if not is_valid_city(message.text):
        return await safe_send(uid, "❌ Некорректное название города. Введите ещё раз:")
    data = await _get_fsm_data(uid)
    city = (message.text or "").strip()
    if city.lower() == data.get("from_city","").lower():
        return await safe_send(uid, "❌ Откуда и куда совпадают. Введите другой город:")
    data["to_city"] = city
    fsm_data[uid] = data
    fsm_data_ts[uid] = asyncio.get_event_loop().time()
    await bot.state_dispenser.set(uid, TripStates.trip_date)
    await save_fsm(uid, TripStates.trip_date, data)
    await safe_send(uid, f"✅ Куда: {city}\n\n📅 Шаг 3/6 — Дата отправления? (ДД.ММ.ГГГГ)")

@bot.on.message(state=TripStates.trip_date)
async def trip_date_handler(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        await _fsm_clear(uid)
        return await safe_send(uid, "❌ Отменено.", keyboard=kb_driver_menu(await has_active_sub(uid)))
    parsed_date = parse_ru_date(message.text)
    if parsed_date is None:
        return await safe_send(uid, "❌ Некорректная дата. Введите в формате ДД.ММ.ГГГГ:")
    data = await _get_fsm_data(uid)
    data["trip_date"] = parsed_date.strftime("%d.%m.%Y")
    fsm_data[uid] = data
    fsm_data_ts[uid] = asyncio.get_event_loop().time()
    await bot.state_dispenser.set(uid, TripStates.trip_time)
    await save_fsm(uid, TripStates.trip_time, data)
    await safe_send(uid, f"✅ Дата: {data['trip_date']}\n\n🕐 Шаг 4/6 — Время отправления? (ЧЧ:ММ)")

@bot.on.message(state=TripStates.trip_time)
async def trip_time_handler(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        await _fsm_clear(uid)
        return await safe_send(uid, "❌ Отменено.", keyboard=kb_driver_menu(await has_active_sub(uid)))
    parsed_time = parse_ru_time(message.text)
    if parsed_time is None:
        return await safe_send(uid, "❌ Некорректное время. Введите ЧЧ:ММ:")
    h, mi = parsed_time
    data = await _get_fsm_data(uid)
    nice_time = f"{h:02d}:{mi:02d}"
    data["trip_time"] = nice_time
    fsm_data[uid] = data
    fsm_data_ts[uid] = asyncio.get_event_loop().time()
    await bot.state_dispenser.set(uid, TripStates.seats_total)
    await save_fsm(uid, TripStates.seats_total, data)
    kb = Keyboard(inline=False)
    for i in range(1, 9):
        kb.add(Text(str(i)), color=KeyboardButtonColor.PRIMARY)
        if i % 2 == 0:
            kb.row()
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    await safe_send(uid, f"✅ Время: {nice_time}\n\n💺 Шаг 5/6 — Сколько мест выставляете?", keyboard=kb.get_json())

@bot.on.message(state=TripStates.seats_total)
async def trip_seats_handler(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        await _fsm_clear(uid)
        return await safe_send(uid, "❌ Отменено.", keyboard=kb_driver_menu(await has_active_sub(uid)))
    try:
        seats = int(message.text)
        if seats < 1 or seats > 8:
            raise ValueError
    except:
        return await safe_send(uid, "❌ Введите число от 1 до 8:")
    data = await _get_fsm_data(uid)
    data["seats_total"] = seats
    region = route_region(data.get("from_city",""), data.get("to_city",""))
    data["region"] = region
    fsm_data[uid] = data
    fsm_data_ts[uid] = asyncio.get_event_loop().time()
    await bot.state_dispenser.set(uid, TripStates.car_class)
    await save_fsm(uid, TripStates.car_class, data)
    t = TARIFFS_CIS if region == "cis" else (TARIFFS_NT if region == "nt" else TARIFFS_RF)
    region_hint = "\n🌍 Тариф: Кавказ/СНГ (цена за место)" if region == "cis" else ""
    kb = Keyboard(inline=False)
    for k, v in t.items():
        kb.add(Text(
            f"{CLASS_LABELS[k]} — {v['price_seat']} ₽/км/место"
        ), color=KeyboardButtonColor.PRIMARY)
        kb.row()
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    await safe_send(uid, 
        f"✅ Мест: {seats}{region_hint}\n\n🚘 Шаг 6/6 — Класс автомобиля:",
        keyboard=kb.get_json()
    )

@bot.on.message(state=TripStates.car_class)
async def trip_car_class_handler(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        await _fsm_clear(uid)
        return await safe_send(uid, "❌ Отменено.", keyboard=kb_driver_menu(await has_active_sub(uid)))
    # Парсим класс из текста кнопки (может содержать суффикс с ценой)
    class_name_map = {v: k for k, v in CLASS_LABELS.items()}
    chosen_class = None
    for label_text, key in class_name_map.items():
        if message.text.startswith(label_text):
            chosen_class = key
            break
    if not chosen_class:
        return await safe_send(uid, "❌ Выберите класс из списка на клавиатуре:")
    data = await _get_fsm_data(uid)
    region = data.get("region", "rf")
    t = TARIFFS_CIS if region == "cis" else (TARIFFS_NT if region == "nt" else TARIFFS_RF)
    # Проверяем что выбранный класс существует в тарифе текущего региона
    if chosen_class not in t:
        return await safe_send(uid, "❌ Выберите класс из списка на клавиатуре:")
    # Нельзя заявить рейс классом ВЫШЕ реально зарегистрированной машины —
    # ниже можно (выбор самого водителя), а "комфорт" не повезёт как "бизнес",
    # и обычный седан не станет минивэном на 5-8 мест.
    async with _pg_pool.acquire() as conn:
        drv = await conn.fetchrow("SELECT car_class FROM drivers WHERE user_id=$1", uid)
    drv_cc = drv["car_class"] if drv and drv["car_class"] else "standard"
    seats_total = data.get("seats_total", 1)
    if chosen_class == "minivan" and drv_cc != "minivan":
        return await safe_send(uid, "❌ У вас не зарегистрирован минивэн — этот класс недоступен.")
    if chosen_class != "minivan" and drv_cc != "minivan":
        if CAR_CLASS_TIER.get(chosen_class, 0) > CAR_CLASS_TIER.get(drv_cc, 0):
            return await safe_send(uid, f"❌ Ваша машина зарегистрирована как «{CLASS_LABELS.get(drv_cc, drv_cc)}» — выше этого класса выбрать нельзя.")
    if chosen_class != "minivan" and seats_total > 4:
        return await safe_send(uid, "❌ Для 5+ мест доступен только минивэн — обычная машина столько не увезёт.")
    data["car_class"] = chosen_class
    data["car_class_label"] = CLASS_LABELS[chosen_class]
    fsm_data[uid] = data
    fsm_data_ts[uid] = asyncio.get_event_loop().time()
    await bot.state_dispenser.set(uid, TripStates.price_per_seat)
    await save_fsm(uid, TripStates.price_per_seat, data)
    # Считаем предлагаемую цену
    await safe_send(uid, "⏳ Рассчитываю расстояние...")
    dist = await calculate_distance_async(data.get("from_city",""), data.get("to_city",""))
    if dist:
        data["distance_km"] = dist
        suggested = calculate_price(dist, chosen_class, data.get("from_city",""), data.get("to_city",""), trip_mode=True)
        rate_full = t[chosen_class]['price']
        rate_seat = t[chosen_class]['price_seat']
        price_text = (
            f"💡 Предлагаемая цена за место: {suggested} ₽\n"
            f"   ({dist} км × {rate_seat} ₽/км/место)\n"
            f"   Для справки — вся машина: {calculate_price(dist, chosen_class, data.get('from_city',''), data.get('to_city',''))} ₽ ({rate_full} ₽/км)"
        )
    else:
        data["distance_km"] = None
        suggested = None
        price_text = "⚠️ Расстояние не удалось рассчитать. Введите цену вручную."
    fsm_data[uid] = data
    await save_fsm(uid, TripStates.price_per_seat, data)
    kb = Keyboard(inline=False)
    if suggested:
        kb.add(Text(f"✅ {suggested} ₽ (рекомендуется)"), color=KeyboardButtonColor.POSITIVE)
        kb.row()
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    await safe_send(uid, 
        f"✅ Класс: {CLASS_LABELS[chosen_class]}\n\n💰 Цена за место:\n{price_text}\n\n"
        f"Введите свою цену (целое число ₽) или нажмите кнопку:",
        keyboard=kb.get_json()
    )

@bot.on.message(state=TripStates.price_per_seat)
async def trip_price_handler(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        await _fsm_clear(uid)
        return await safe_send(uid, "❌ Отменено.", keyboard=kb_driver_menu(await has_active_sub(uid)))
    data = await _get_fsm_data(uid)
    # Проверяем нажатие кнопки с рекомендованной ценой
    price_text = message.text
    if price_text.startswith("✅ "):
        price_text = price_text.replace("✅ ","").split(" ₽")[0]
    try:
        price = int(price_text.replace(" ","").replace("₽",""))
        if price < 100:
            return await safe_send(uid, "❌ Минимальная цена 100 ₽. Введите снова:")
    except:
        return await safe_send(uid, "❌ Введите целое число (цена в рублях):")
    data["price_per_seat"] = price
    fsm_data[uid] = data
    # Публикуем рейс
    required = ("from_city","to_city","trip_date","trip_time","seats_total","car_class","car_class_label","region")
    if any(k not in data for k in required):
        await _fsm_clear(uid)
        return await safe_send(uid, "⚠️ Данные сессии потеряны. Начните создание рейса заново.", keyboard=kb_driver_menu(await has_active_sub(uid)))
    try:
        trip_date = datetime.strptime(data["trip_date"], "%d.%m.%Y").date()
        trip_time = datetime.strptime(data["trip_time"], "%H:%M").time()
        async with _pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO trips
                   (driver_id,from_city,to_city,trip_date,trip_time,
                    car_class,car_class_label,seats_total,seats_free,
                    price_per_seat,distance_km,region,status)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$8,$9,$10,$11,'open') RETURNING id""",
                uid, data["from_city"], data["to_city"],
                trip_date, trip_time,
                data["car_class"], data["car_class_label"],
                data["seats_total"], price, data.get("distance_km"), data["region"]
            )
        tid = row["id"]
        await _fsm_clear(uid)
        dist_km = data.get("distance_km")
        dist_str = f"\n📏 {int(dist_km)} км" if dist_km else ""
        await safe_send(uid, 
            f"🎉 Рейс #{tid} опубликован!\n\n"
            f"📍 {data['from_city']} → {data['to_city']}\n"
            f"📅 {data['trip_date']} · {data['trip_time']}{dist_str}\n"
            f"🚘 {data['car_class_label']}\n"
            f"💺 Мест: {data['seats_total']}\n"
            f"💰 {price} ₽/место\n\n"
            f"Пассажиры найдут его при поиске маршрута.",
            keyboard=kb_driver_menu(await has_active_sub(uid))
        )
    except Exception as e:
        log.error(f"trip_price_handler uid={uid}: {e}")
        await safe_send(uid, "❌ Ошибка при создании рейса. Попробуйте ещё раз.", keyboard=kb_driver_menu(await has_active_sub(uid)))

# Водитель смотрит свои рейсы
@bot.on.message(text="🗓 Мои рейсы")
async def my_trips_handler(message: Message):
    uid = message.from_id
    if await check_blacklist(uid):
        return await safe_send(uid, "⛔ Вы заблокированы.")
    if not await is_driver_registered(uid):
        return await safe_send(uid, "❌ Вы не зарегистрированы как водитель.")
    async with _pg_pool.acquire() as conn:
        trips = await conn.fetch(
            "SELECT * FROM trips WHERE driver_id=$1 ORDER BY trip_date DESC, trip_time DESC LIMIT 20", uid
        )
    if not trips:
        return await safe_send(uid, "📭 У вас ещё нет рейсов.", keyboard=kb_driver_menu(await has_active_sub(uid)))
    await safe_send(uid, f"🗓 Ваши рейсы ({len(trips)}):")
    for t in trips:
        bookings_count = 0
        async with _pg_pool.acquire() as conn:
            bc = await conn.fetchval("SELECT COUNT(*) FROM trip_bookings WHERE trip_id=$1 AND status='confirmed'", t["id"])
            bookings_count = bc or 0
        maps_url = yandex_maps_url(t['from_city'], t['to_city'])
        kb = Keyboard(inline=True)
        if t["status"] in ("open","full"):
            kb.add(
                Text(f"👥 Пассажиры ({bookings_count})", payload={"cmd":"trip_passengers","trip_id":t["id"]}),
                color=KeyboardButtonColor.SECONDARY
            )
            kb.row()
            kb.add(OpenLink(label="🗺 Маршрут", link=maps_url))
            kb.row()
            kb.add(
                Text("✅ Завершить рейс", payload={"cmd":"trip_complete","trip_id":t["id"]}),
                color=KeyboardButtonColor.POSITIVE
            )
            kb.row()
            kb.add(
                Text("❌ Отменить рейс", payload={"cmd":"trip_cancel","trip_id":t["id"]}),
                color=KeyboardButtonColor.NEGATIVE
            )
        await safe_send(uid, fmt_trip(t), keyboard=kb.get_json() if t["status"] in ("open","full") else None)
        await asyncio.sleep(0.05)

# ================= РЕЙСЫ — ПАССАЖИР ИЩЕТ МЕСТО =================

@bot.on.message(text="🚐 Поехать вместе")
async def search_trip_start(message: Message):
    uid = message.from_id
    if await check_blacklist(uid):
        return await safe_send(uid, "⛔ Вы заблокированы.")
    await bot.state_dispenser.set(uid, SearchTripStates.from_city)
    fsm_data[uid] = {}
    fsm_data_ts[uid] = asyncio.get_event_loop().time()
    await save_fsm(uid, SearchTripStates.from_city, {})
    await safe_send(uid, "🚐 Поиск рейса\n\n🏙 Шаг 1/3 — Откуда едете?", keyboard=kb_cancel())

@bot.on.message(state=SearchTripStates.from_city)
async def search_from_city(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        await _fsm_clear(uid)
        return await safe_send(uid, "❌ Поиск отменён.", keyboard=kb_main(uid))
    if not is_valid_city(message.text):
        return await safe_send(uid, "❌ Некорректное название города.")
    data = {"from_city": (message.text or "").strip()}
    fsm_data[uid] = data
    fsm_data_ts[uid] = asyncio.get_event_loop().time()
    await bot.state_dispenser.set(uid, SearchTripStates.to_city)
    await save_fsm(uid, SearchTripStates.to_city, data)
    await safe_send(uid, f"✅ Откуда: {data['from_city']}\n\n🏙 Шаг 2/3 — Куда?")

@bot.on.message(state=SearchTripStates.to_city)
async def search_to_city(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        await _fsm_clear(uid)
        return await safe_send(uid, "❌ Поиск отменён.", keyboard=kb_main(uid))
    if not is_valid_city(message.text):
        return await safe_send(uid, "❌ Некорректное название города.")
    data = await _get_fsm_data(uid)
    data["to_city"] = (message.text or "").strip()
    fsm_data[uid] = data
    fsm_data_ts[uid] = asyncio.get_event_loop().time()
    await bot.state_dispenser.set(uid, SearchTripStates.trip_date)
    await save_fsm(uid, SearchTripStates.trip_date, data)
    await safe_send(uid, f"✅ Куда: {data['to_city']}\n\n📅 Шаг 3/3 — Дата? (ДД.ММ.ГГГГ)")

@bot.on.message(state=SearchTripStates.trip_date)
async def search_date_handler(message: Message):
    uid = message.from_id
    if message.text == "❌ Отменить":
        await _fsm_clear(uid)
        return await safe_send(uid, "❌ Поиск отменён.", keyboard=kb_main(uid))
    trip_date = parse_ru_date(message.text)
    if trip_date is None:
        return await safe_send(uid, "❌ Некорректная дата. Введите ДД.ММ.ГГГГ:")
    data = await _get_fsm_data(uid)
    await _fsm_clear(uid)
    from_city = data.get("from_city","")
    to_city   = data.get("to_city","")
    nice_date = trip_date.strftime("%d.%m.%Y")
    await safe_send(uid, "🔍 Ищу рейсы...", keyboard=kb_main(uid))
    async with _pg_pool.acquire() as conn:
        trips = await conn.fetch(
            "SELECT * FROM trips WHERE LOWER(from_city)=LOWER($1) AND LOWER(to_city)=LOWER($2) "
            "AND trip_date=$3 AND status='open' AND seats_free>0 ORDER BY trip_time ASC",
            from_city, to_city, trip_date
        )
    if not trips:
        return await safe_send(uid, 
            f"😔 Рейсов {from_city} → {to_city} на {nice_date} не найдено.\n"
            "Попробуйте другую дату или создайте обычный заказ."
        )
    await safe_send(uid, f"✅ Найдено рейсов: {len(trips)}\n📍 {from_city} → {to_city} · {nice_date}")
    for t in trips:
        try:
            vk_u = await bot.api.users.get(user_ids=[t["driver_id"]])
            drv_name = f"{vk_u[0].first_name} {vk_u[0].last_name}" if vk_u else f"id{t['driver_id']}"
        except:
            drv_name = f"id{t['driver_id']}"
        avg, cnt = await avg_rating(t["driver_id"])
        rating_str = f"⭐ {avg}/5 ({cnt} оц.)" if cnt else "⭐ Новый водитель"
        maps_url = yandex_maps_url(t['from_city'], t['to_city'])
        kb = Keyboard(inline=True)
        kb.add(
            Text("📌 Забронировать место", payload={"cmd":"book_trip","trip_id":t["id"]}),
            color=KeyboardButtonColor.POSITIVE
        )
        kb.row()
        kb.add(OpenLink(label="🗺 Маршрут", link=maps_url))
        await safe_send(uid,
            f"{fmt_trip(t)}\n👤 {vk_link(t['driver_id'], drv_name)} · {rating_str}",
            keyboard=kb.get_json()
        )
        await asyncio.sleep(0.05)
    await safe_send(uid, "⬆️ Выберите рейс и нажмите «Забронировать»")

# Просмотр своих броней
@bot.on.message(text="🎫 Мои брони")
async def my_bookings_handler(message: Message):
    uid = message.from_id
    if await check_blacklist(uid):
        return await safe_send(uid, "⛔ Вы заблокированы.")
    async with _pg_pool.acquire() as conn:
        bookings = await conn.fetch(
            """SELECT tb.*, t.from_city, t.to_city, t.trip_date, t.trip_time,
                      t.price_per_seat, t.car_class_label, t.driver_id, t.status as trip_status
               FROM trip_bookings tb JOIN trips t ON t.id=tb.trip_id
               WHERE tb.passenger_id=$1 AND tb.status='confirmed'
               ORDER BY t.trip_date DESC, t.trip_time DESC LIMIT 10""",
            uid
        )
    if not bookings:
        return await safe_send(uid, "📭 У вас нет активных броней на рейсы.", keyboard=kb_main(uid))
    await safe_send(uid, f"🎫 Ваши брони ({len(bookings)}):")
    status_icon = {"open":"🟢 Активен","full":"🔵 Набран","cancelled":"🔴 Отменён"}
    for b in bookings:
        trip_date = b['trip_date'].strftime('%d.%m.%Y') if hasattr(b['trip_date'],'strftime') else str(b['trip_date'])
        trip_time = b['trip_time'].strftime('%H:%M') if hasattr(b['trip_time'],'strftime') else str(b['trip_time'])
        icon = status_icon.get(b.get("trip_status","open"), "—")
        maps_url = yandex_maps_url(b['from_city'], b['to_city'])
        kb = Keyboard(inline=True)
        kb.add(OpenLink(label="📞 Написать водителю", link=f"https://vk.com/id{b['driver_id']}"))
        kb.row()
        kb.add(OpenLink(label="🗺 Маршрут", link=maps_url))
        kb.row()
        kb.add(
            Text("ℹ️ Инфо о рейсе", payload={"cmd":"trip_info","trip_id":b["trip_id"]}),
            color=KeyboardButtonColor.SECONDARY
        )
        kb.add(
            Text("👥 Пассажиры рейса", payload={"cmd":"trip_passengers_public","trip_id":b["trip_id"]}),
            color=KeyboardButtonColor.SECONDARY
        )
        if b.get("trip_status") in ("open","full"):
            kb.row()
            kb.add(
                Text("❌ Отменить бронь", payload={"cmd":"cancel_booking","trip_id":b["trip_id"]}),
                color=KeyboardButtonColor.NEGATIVE
            )
        await safe_send(uid,
            f"🎫 Бронь #{b['id']} · {icon}\n"
            f"🚐 Рейс #{b['trip_id']}\n"
            f"📍 {esc(b['from_city'])} → {esc(b['to_city'])}\n"
            f"📅 {trip_date} · 🕐 {trip_time}\n"
            f"🚘 {esc(b['car_class_label'])}\n"
            f"💺 Мест: {b['seats']}\n"
            f"💰 К оплате: {b['price_per_seat'] * b['seats']} ₽",
            keyboard=kb.get_json()
        )
        await asyncio.sleep(0.05)

# ================= PAYLOAD ОБРАБОТЧИКИ ДЛЯ РЕЙСОВ =================
# Добавляются в unified_payload_handler через расширение блока if/elif

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

# ══════════════ WEB API (резерв под сайт мтранс.рф, если Telegram заблокируют) ══════════════
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

api = FastAPI(title="МТранс VK Order API")

api.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://мтранс.рф", "https://xn--80axekhc.xn--p1ai",
        "https://www.мтранс.рф", "https://www.xn--80axekhc.xn--p1ai",
    ],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

VALID_CAR_CLASSES = {"standard", "comfort", "comfort_plus", "minivan", "business"}

@api.get("/")
async def root():
    return {"service": "МТранс VK Order API", "status": "ok"}

@api.get("/health")
async def health():
    ok_db = _pg_pool is not None
    return {"status": "ok" if ok_db else "no_db", "db_pool": ok_db}

class SiteOrderRequest(BaseModel):
    from_city: str
    to_city: str
    trip_date: str            # "YYYY-MM-DD"
    trip_time: str = ""       # "HH:MM"
    passengers: int = Field(1, ge=1, le=8)
    car_class: str = "standard"
    name: str = ""
    phone: str
    wishes: str = ""

@api.post("/api/order")
async def create_site_order(req: SiteOrderRequest):
    if req.car_class not in VALID_CAR_CLASSES:
        raise HTTPException(400, "Некорректный класс авто")
    if not req.phone.strip():
        raise HTTPException(400, "Укажите телефон")
    try:
        trip_date = datetime.strptime(req.trip_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Некорректная дата (ожидается YYYY-MM-DD)")
    if trip_date < datetime.now().date():
        raise HTTPException(400, "Дата поездки не может быть в прошлом")
    trip_time = None
    if req.trip_time:
        try:
            trip_time = datetime.strptime(req.trip_time, "%H:%M").time()
        except ValueError:
            raise HTTPException(400, "Некорректное время (ожидается HH:MM)")
    if trip_time is None:
        trip_time = datetime.strptime("12:00", "%H:%M").time()

    dist = await calculate_distance_async(req.from_city, req.to_city)
    price = calculate_price(dist, req.car_class, req.from_city, req.to_city) if dist else None

    # ВАЖНО: в Python оператор % всегда даёт результат со знаком делителя,
    # поэтому -int(...) % N был БАГОМ и давал положительное число (та же
    # ошибка, что нашлась и исправлена в Telegram-боте).
    site_passenger_id = -secrets.randbelow(9_000_000_000) - 1
    contact_note = f"📞 {req.phone}" + (f", {req.name}" if req.name else "")
    full_wishes = (req.wishes + "\n" if req.wishes else "") + f"🌐 Заявка с сайта. {contact_note}"
    token = secrets.token_urlsafe(12)

    async with _pg_pool.acquire() as conn:
        order = await conn.fetchrow(
            """INSERT INTO orders(
                passenger_id, from_city, to_city, trip_date, trip_time,
                passengers, car_class, wishes, distance_km, price, cancel_token
            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            RETURNING id""",
            site_passenger_id, req.from_city, req.to_city, trip_date, trip_time,
            req.passengers, req.car_class, full_wishes, dist, price, token
        )
        oid = order["id"]

    if dist:
        asyncio.create_task(notify_drivers_about_order(oid))
    else:
        for aid in ADMIN_IDS:
            await safe_send(aid, f"⚠️ Заявка с сайта #{oid} без расстояния!\n{req.from_city} → {req.to_city}")

    return {
        "order_id": oid,
        "distance_km": dist,
        "price": price,
        "status": "open",
        "cancel_token": token,
    }

@api.get("/api/order/{oid}")
async def get_site_order(oid: int):
    order = await get_order(oid)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    return {
        "order_id": order["id"], "status": order["status"],
        "price": order["price"], "distance_km": order["distance_km"],
        "has_driver": bool(order.get("driver_id")),
    }

class EstimateRequest(BaseModel):
    from_city: str
    to_city: str
    car_class: str = "standard"

@api.post("/api/estimate")
async def estimate_price(req: EstimateRequest):
    if req.car_class not in VALID_CAR_CLASSES:
        raise HTTPException(400, "Некорректный класс авто")
    if not req.from_city.strip() or not req.to_city.strip():
        raise HTTPException(400, "Укажите города")
    dist = await calculate_distance_async(req.from_city, req.to_city)
    if not dist:
        return {"distance_km": None, "price": None}
    price = calculate_price(dist, req.car_class, req.from_city, req.to_city)
    return {"distance_km": dist, "price": price}

class CancelRequest(BaseModel):
    token: str

@api.post("/api/order/{oid}/cancel")
async def cancel_site_order(oid: int, req: CancelRequest):
    async with _pg_pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE orders SET status='cancelled' WHERE id=$1 AND cancel_token=$2 "
            "AND status IN ('open','taken')",
            oid, req.token)
    if int(result.split()[-1]) == 0:
        raise HTTPException(400, "Не удалось отменить: неверный токен или заказ уже закрыт")
    order = await get_order(oid)
    if order and order.get("driver_id"):
        await safe_send(order["driver_id"], f"❌ Заявка #{oid} отменена пассажиром (с сайта)")
    return {"status": "cancelled"}


async def main():
    await init_pg()
    # Регистрируем middleware для восстановления FSM ДО роутинга
    bot.labeler.message_view.register_middleware(FSMRestoreMiddleware)
    asyncio.create_task(fsm_cleanup_task())
    asyncio.create_task(auto_cancel_expired_orders())

    import uvicorn
    api_port = int(os.environ.get("PORT", 8080))

    async def _run_api_safely():
        """Веб-API не должен ронять весь процесс (включая VK-бота), если порт
        занят или сервер по другой причине не поднялся. Несколько попыток —
        вдруг порт освобождается с задержкой после передеплоя."""
        for attempt in range(1, 6):
            try:
                api_config = uvicorn.Config(api, host="0.0.0.0", port=api_port, log_level="warning")
                api_server = uvicorn.Server(api_config)
                await api_server.serve()
                return
            except SystemExit as e:
                log.error(f"⚠️ Попытка {attempt}/5: Web API не поднялся (SystemExit {e.code}), "
                          f"порт {api_port} занят.")
            except Exception as e:
                log.error(f"⚠️ Попытка {attempt}/5: Web API упал: {e}")
            if attempt < 5:
                await asyncio.sleep(3)
        log.error("❌ Web API так и не запустился за 5 попыток. Бот продолжает работать без API.")

    asyncio.create_task(_run_api_safely())
    log.info(f"🌐 Web API (резерв под сайт) запущен на порту {api_port}")

    log.info("🚀 Бот Межгород Трансфер (ВК) успешно запущен!")
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
