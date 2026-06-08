import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple
import html
import random

import asyncpg
from geopy.geocoders import Yandex
from geopy.distance import geodesic as geo_dist

from vkbottle.bot import Bot, Message
from vkbottle import BaseStateGroup, Keyboard, KeyboardButtonColor, Text

# === Настройки из env ===
VK_TOKEN = os.getenv("VK_TOKEN")
YANDEX_GEO_KEY = os.getenv("YANDEX_GEOCODER_KEY")
PG_DSN = os.getenv("PG_DSN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))
TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))
DIST_COEFF = 1.25

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(token=VK_TOKEN)
geolocator = Yandex(api_key=YANDEX_GEO_KEY) if YANDEX_GEO_KEY else None

_pg_pool = None

# === Клавиатуры ===
def kb_main(user_id=None):
    kb = Keyboard(inline=False)
    kb.add(Text("🚕 Создать заказ"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("🚗 Я водитель"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("📋 Мои заказы"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("📊 Тарифы"), color=KeyboardButtonColor.SECONDARY).row()
    kb.add(Text("💳 Абонемент"), color=KeyboardButtonColor.SECONDARY)
    if user_id in ADMIN_IDS:
        kb.add(Text("🔧 Админ"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_driver_menu():
    kb = Keyboard(inline=False)
    kb.add(Text("📈 Мои поездки"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("👤 Мой профиль"), color=KeyboardButtonColor.SECONDARY).row()
    kb.add(Text("📊 Моя статистика"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("🔙 Назад"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_admin_menu():
    kb = Keyboard(inline=False)
    kb.add(Text("📋 Заказы"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("👥 Пользователи"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("✅ Верификация"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("💳 Подписки"), color=KeyboardButtonColor.SECONDARY).row()
    kb.add(Text("📊 Статистика"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("🔙 Назад"), color=KeyboardButtonColor.NEGATIVE)
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

# === FSM States ===
class OrderStates(BaseStateGroup):
    from_city = "from_city"
    to_city = "to_city"
    trip_date = "trip_date"
    trip_time = "trip_time"
    passengers = "passengers"
    car_class = "car_class"
    wishes = "wishes"

class DriverStates(BaseStateGroup):
    phone = "phone"
    car_model = "car_model"
    car_year = "car_year"
    car_number = "car_number"
    car_class = "car_class"

# === Подписки ===
SUBS = {
    "60": {"days": 60, "price": 650, "label": "60 дней — 650 ₽"},
    "120": {"days": 120, "price": 1100, "label": "120 дней — 1 100 ₽"},
    "240": {"days": 240, "price": 2000, "label": "240 дней — 2 000 ₽"},
    "365": {"days": 365, "price": 3500, "label": "1 год — 3 500 ₽"},
}

# === Тарифы ===
TARIFFS = {
    "standard": {"name": "Стандарт", "price_per_km": 15, "min_price": 500},
    "comfort": {"name": "Комфорт", "price_per_km": 20, "min_price": 800},
    "comfort+": {"name": "Комфорт+", "price_per_km": 25, "min_price": 1000},
    "minivan": {"name": "Минивэн", "price_per_km": 22, "min_price": 900},
    "business": {"name": "Бизнес", "price_per_km": 30, "min_price": 1500},
}

# === Инициализация базы и таблиц ===
async def init_pg():
    global _pg_pool
    _pg_pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=10)
    async with _pg_pool.acquire() as conn:
        # Основные таблицы с правильными типами данных
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
                price INT CHECK(price > 0),
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
                car_year INT CHECK(car_year BETWEEN 1990 AND 2026),
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
        
        # Индексы для оптимизации запросов
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_passenger_id ON orders(passenger_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_driver_id ON orders(driver_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC)
        """)

# === Утилиты / Валидации ===
def esc(text) -> str:
    return html.escape(str(text)) if text else "—"

def is_valid_city(city: str) -> bool:
    return bool(city) and len(city.strip()) >= 2 and not city.strip().isdigit()

def is_valid_date(text: str) -> bool:
    try:
        d = datetime.strptime(text, "%d.%m.%Y")
        today = datetime.now(TZ).date()
        return today <= d.date() <= today + timedelta(days=365)
    except:
        return False

def is_valid_time(text: str) -> bool:
    if not re.match(r"^\d{1,2}:\d{2}$", text):
        return False
    try:
        h, m = map(int, text.split(":"))
        return 0 <= h <= 23 and 0 <= m <= 59
    except:
        return False

def is_valid_phone(text: str) -> bool:
    cleaned = text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    return bool(re.match(r"^\+?[78][0-9]{10}$", cleaned))

def is_valid_car_number(text: str) -> bool:
    return bool(re.match(r"^[АВЕКМНОРСТУХавекмнорстух]\d{3}[АВЕКМНОРСТУХавекмнорстух]{2}\d{2,3}$", text))

def geocode(city: str) -> Optional[Tuple[float, float]]:
    if not geolocator or not city:
        return None
    try:
        loc = geolocator.geocode(city, timeout=5)
        if loc: 
            return loc.latitude, loc.longitude
    except Exception as ex:
        log.error(f"Geocode error for '{city}': {ex}")
    return None

def calculate_distance(city_from: str, city_to: str) -> Optional[float]:
    coords_from = geocode(city_from)
    coords_to = geocode(city_to)
    if coords_from and coords_to:
        distance = geo_dist(coords_from, coords_to).kilometers
        return round(distance * DIST_COEFF, 1)
    return None

def calculate_price(distance_km: Optional[float], car_class: str) -> int:
    if not distance_km or car_class not in TARIFFS:
        return 0
    tariff = TARIFFS[car_class]
    price = distance_km * tariff['price_per_km']
    return round(max(price, tariff['min_price']))

async def check_blacklist(user_id: int) -> bool:
    async with _pg_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT 1 FROM blacklist WHERE user_id=$1", user_id)
    return r is not None

def can_take_order(driver_class: str, order_class: str) -> Tuple[bool, str]:
    level = {"standard": 0, "comfort": 1, "comfort+": 2, "minivan": 3, "business": 4}
    d_lvl = level.get(driver_class, 0)
    o_lvl = level.get(order_class, 0)
    
    if order_class == "minivan" and driver_class != "minivan":
        return False, "Заказы минивэна — только для минивэнов"
    if order_class == "business" and driver_class != "business":
        return False, "Заказы бизнес-класса — только для бизнес-авто"
    if d_lvl < o_lvl:
        return False, f"Ваш класс ({driver_class}) ниже требуемого ({order_class})"
    return True, ""

def fmt_order(o: Dict) -> str:
    status_map = {
        'open': '🟢 Открыт',
        'taken': '🟡 Принят',
        'completed': '✅ Завершён',
        'cancelled': '❌ Отменён'
    }
    lines = [
        f"🚕 Заказ #{o['id']}",
        f"📍 {esc(o['from_city'])} → {esc(o['to_city'])}",
        f"📅 {o['trip_date']} 🕐 {o['trip_time']}",
        f"👥 Пассажиров: {o['passengers']}",
        f"🚘 Класс: {esc(o['car_class'])}",
        f"📏 Расстояние: {o.get('distance_km', '—')} км",
        f"💰 Цена: {o.get('price', '—')} ₽",
        f"Статус: {status_map.get(o['status'], o['status'])}",
    ]
    if o.get('wishes'):
        lines.append(f"💬 Пожелания: {esc(o['wishes'])}")
    return "\n".join(lines)

async def safe_send(user_id: int, text: str, keyboard=None):
    try:
        await bot.api.messages.send(
            user_id=user_id, 
            message=text, 
            keyboard=keyboard,
            random_id=random.randint(1, 2**31-1)
        )
    except Exception as e:
        log.error(f"Ошибка отправки пользователю {user_id}: {e}")

async def has_active_sub(user_id: int) -> bool:
    async with _pg_pool.acquire() as conn:
        r = await conn.fetchrow("SELECT expires_date FROM subscriptions WHERE user_id=$1", user_id)
        if not r or not r['expires_date']:
            return False
        try:
            if isinstance(r['expires_date'], str):
                exp = datetime.strptime(r['expires_date'], "%Y-%m-%d").date()
            else:
                exp = r['expires_date']
            return exp >= datetime.now(TZ).date()
        except:
            return False

async def is_driver_registered(user_id: int) -> bool:
    async with _pg_pool.acquire() as conn:
        drv = await conn.fetchrow("SELECT 1 FROM drivers WHERE user_id=$1", user_id)
        return drv is not None

async def is_driver_verified(user_id: int) -> bool:
    async with _pg_pool.acquire() as conn:
        drv = await conn.fetchrow("SELECT docs_verified FROM drivers WHERE user_id=$1", user_id)
        return drv and drv['docs_verified']

async def is_driver_busy(driver_id: int) -> bool:
    """Проверяет, есть ли у водителя активные заказы"""
    async with _pg_pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT COUNT(*) as cnt FROM orders WHERE driver_id=$1 AND status='taken'",
            driver_id
        )
        return r['cnt'] > 0

async def get_order(order_id: int) -> Optional[Dict]:
    async with _pg_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)

async def update_order_status(order_id: int, status: str, driver_id=None):
    async with _pg_pool.acquire() as conn:
        if driver_id is not None:
            await conn.execute(
                "UPDATE orders SET status=$1, driver_id=$2, updated_at=NOW() WHERE id=$3",
                status, driver_id, order_id
            )
        else:
            await conn.execute(
                "UPDATE orders SET status=$1, updated_at=NOW() WHERE id=$2",
                status, order_id
            )

async def try_take_order_atomic(order_id: int, driver_id: int) -> Tuple[bool, str, Optional[Dict]]:
    """Атомарно пытается взять заказ с блокировкой строки"""
    async with _pg_pool.acquire() as conn:
        async with conn.transaction():
            # Блокируем строку для предотвращения гонки состояний
            order = await conn.fetchrow(
                "SELECT * FROM orders WHERE id=$1 FOR UPDATE",
                order_id
            )
            
            if not order:
                return False, "Заказ не найден", None
            if order['status'] != 'open':
                return False, "Заказ уже принят или отменён", None
            if order['passenger_id'] == driver_id:
                return False, "Вы не можете взять свой заказ", None
            
            await conn.execute(
                "UPDATE orders SET status='taken', driver_id=$1, updated_at=NOW() WHERE id=$2",
                driver_id, order_id
            )
            return True, "", order

async def has_rating(order_id: int, passenger_id: int) -> bool:
    async with _pg_pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT 1 FROM ratings WHERE order_id=$1 AND passenger_id=$2", 
            order_id, passenger_id
        )
    return r is not None

async def add_rating(order_id: int, driver_id: int, passenger_id: int, stars: int):
    async with _pg_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ratings(order_id, driver_id, passenger_id, stars, created_at) 
               VALUES($1,$2,$3,$4,$5)
               ON CONFLICT(order_id, passenger_id) 
               DO UPDATE SET stars=$4, created_at=$5""",
            order_id, driver_id, passenger_id, stars, datetime.now(TZ)
        )

async def avg_rating(driver_id: int) -> Tuple[float, int]:
    async with _pg_pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT COALESCE(AVG(stars)::float, 0) as avg, COUNT(*)::int as cnt FROM ratings WHERE driver_id=$1", 
            driver_id
        )
    if r and r['cnt'] > 0:
        return round(r['avg'], 1), r['cnt']
    return 0.0, 0

async def notify_drivers_about_order(order_id: int):
    async with _pg_pool.acquire() as conn:
        drivers = await conn.fetch("SELECT user_id, car_class FROM drivers WHERE docs_verified=TRUE")
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
        # Проверяем, не занят ли водитель
        if await is_driver_busy(d['user_id']):
            continue
            
        allowed, reason = can_take_order(d['car_class'], order['car_class'])
        if not allowed:
            continue
            
        await safe_send(d['user_id'], text_notify, keyboard=kjson)
        sent_count += 1
        await asyncio.sleep(0.1)
    
    log.info(f"Уведомление о заказе #{order_id} отправлено {sent_count} водителям")

# === Начало работы ===
@bot.on.message(text=["/start", "Начать", "🔙 Назад"])
async def start_handler(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы и не можете использовать этого бота.")
        return
    
    # Сбрасываем все состояния FSM при возврате в главное меню
    await bot.state_dispenser.delete(message.from_id)
    
    welcome_text = (
        "🚕 Добро пожаловать в сервис междугородних поездок!\n\n"
        "Я помогу вам найти попутную машину или заказать поездку между городами.\n\n"
        "Выберите действие в меню:"
    )
    await message.answer(welcome_text, keyboard=kb_main(message.from_id))

# === Создание заказа (FSM) с использованием встроенного state_dispenser ===
@bot.on.message(text="🚕 Создать заказ")
async def start_order(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы и не можете использовать этого бота.")
        return
    
    await bot.state_dispenser.set(message.from_id, OrderStates.from_city, {})
    await message.answer(
        "📍 Введите город отправления:",
        keyboard=kb_cancel()
    )

@bot.on.message(state=OrderStates.from_city)
async def order_from_city(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(message.from_id))
        return
    
    if not is_valid_city(message.text):
        await message.answer("❌ Некорректное название города. Введите ещё раз:")
        return

    city = message.text.strip()
    # Проверяем, что город существует в геокодере
    if geolocator and not geocode(city):
        await message.answer("❌ Город не найден на карте. Проверьте название и введите ещё раз:")
        return
    
    data = {"from_city": city}
    await bot.state_dispenser.set(message.from_id, OrderStates.to_city, data)
    await message.answer("📍 Введите город назначения:")

@bot.on.message(state=OrderStates.to_city)
async def order_to_city(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(message.from_id))
        return
    
    if not is_valid_city(message.text):
        await message.answer("❌ Некорректное название города. Введите ещё раз:")
        return

    data = await bot.state_dispenser.get(message.from_id) or {}
    from_city = data.get("from_city", "")
    city = message.text.strip()
    
    if city.lower() == from_city.lower():
        await message.answer("❌ Город отправления и назначения не должны совпадать. Введите другой город:")
        return
    
    if geolocator and not geocode(city):
        await message.answer("❌ Город не найден на карте. Проверьте название и введите ещё раз:")
        return

    data["to_city"] = city
    await bot.state_dispenser.set(message.from_id, OrderStates.trip_date, data)
    await message.answer("📅 Введите дату поездки в формате ДД.ММ.ГГГГ:")

@bot.on.message(state=OrderStates.trip_date)
async def order_trip_date(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(message.from_id))
        return
    
    if not is_valid_date(message.text):
        await message.answer("❌ Некорректная дата. Введите дату в формате ДД.ММ.ГГГГ (не ранее сегодня и не позже года):")
        return

    data = await bot.state_dispenser.get(message.from_id) or {}
    data["trip_date"] = message.text.strip()
    await bot.state_dispenser.set(message.from_id, OrderStates.trip_time, data)
    await message.answer("🕐 Введите время поездки в формате ЧЧ:ММ:")

@bot.on.message(state=OrderStates.trip_time)
async def order_trip_time(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(message.from_id))
        return
    
    if not is_valid_time(message.text):
        await message.answer("❌ Некорректное время. Введите время в формате ЧЧ:ММ (например, 14:30):")
        return

    data = await bot.state_dispenser.get(message.from_id) or {}
    trip_date_str = data.get("trip_date")
    
    # Проверяем, что дата+время не в прошлом
    try:
        trip_datetime = datetime.strptime(f"{trip_date_str} {message.text}", "%d.%m.%Y %H:%M")
        trip_datetime = trip_datetime.replace(tzinfo=TZ)
        now = datetime.now(TZ)
        if trip_datetime < now:
            await message.answer("❌ Время поездки не может быть в прошлом. Введите корректное время:")
            return
    except Exception:
        await message.answer("❌ Ошибка при обработке даты и времени. Введите корректное время:")
        return

    data["trip_time"] = message.text.strip()
    await bot.state_dispenser.set(message.from_id, OrderStates.passengers, data)
    await message.answer("👥 Введите количество пассажиров (1-8):")

@bot.on.message(state=OrderStates.passengers)
async def order_passengers(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(message.from_id))
        return
    
    try:
        passengers = int(message.text)
        if passengers < 1 or passengers > 8:
            raise ValueError
    except:
        await message.answer("❌ Введите число от 1 до 8:")
        return
    
    data = await bot.state_dispenser.get(message.from_id) or {}
    data["passengers"] = passengers
    
    kb = Keyboard(inline=False)
    kb.add(Text("Стандарт"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("Комфорт"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("Комфорт+"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("Минивэн"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("Бизнес"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    
    await bot.state_dispenser.set(message.from_id, OrderStates.car_class, data)
    await message.answer("🚘 Выберите класс автомобиля:", keyboard=kb.get_json())

@bot.on.message(state=OrderStates.car_class)
async def order_car_class(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(message.from_id))
        return
    
    class_map = {
        "стандарт": "standard",
        "комфорт": "comfort", 
        "комфорт+": "comfort+",
        "минивэн": "minivan",
        "бизнес": "business"
    }
    
    car_class = class_map.get(message.text.lower())
    if not car_class:
        await message.answer("❌ Выберите класс из списка на клавиатуре:")
        return
    
    data = await bot.state_dispenser.get(message.from_id) or {}
    data["car_class"] = car_class
    await bot.state_dispenser.set(message.from_id, OrderStates.wishes, data)
    await message.answer(
        "💬 Введите дополнительные пожелания (или нажмите 'Пропустить'):",
        keyboard=kb_skip()
    )

@bot.on.message(state=OrderStates.wishes)
async def order_wishes(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Создание заказа отменено.", keyboard=kb_main(message.from_id))
        return
    
    data = await bot.state_dispenser.get(message.from_id) or {}
    wishes = message.text if message.text != "Пропустить" else ""
    
    # Рассчитываем расстояние и цену
    distance = calculate_distance(data['from_city'], data['to_city'])
    price = calculate_price(distance, data['car_class']) if distance else 0
    
    # Если не удалось определить расстояние - предупреждаем, но создаём заказ
    if distance is None:
        await message.answer("⚠️ Не удалось определить точное расстояние между городами. Цена может быть неточной.")
    
    async with _pg_pool.acquire() as conn:
        order = await conn.fetchrow(
            """INSERT INTO orders(
                passenger_id, from_city, to_city, trip_date, trip_time,
                passengers, car_class, wishes, distance_km, price
            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            RETURNING id""",
            message.from_id, data['from_city'], data['to_city'],
            data['trip_date'], data['trip_time'], data['passengers'],
            data['car_class'], wishes, distance, price
        )
        order_id = order['id']
    
    await bot.state_dispenser.delete(message.from_id)
    
    confirm_text = (
        f"✅ Заказ #{order_id} создан!\n\n"
        f"📍 {data['from_city']} → {data['to_city']}\n"
        f"📅 {data['trip_date']} в {data['trip_time']}\n"
        f"👥 Пассажиров: {data['passengers']}\n"
        f"🚘 Класс: {data['car_class']}\n"
        f"📏 Расстояние: {distance or '—'} км\n"
        f"💰 Цена: {price if price else '—'} ₽\n"
    )
    if wishes:
        confirm_text += f"💬 Пожелания: {wishes}\n"
    confirm_text += "\n🔍 Ищем подходящих водителей..."
    
    await message.answer(confirm_text, keyboard=kb_main(message.from_id))
    
    asyncio.create_task(notify_drivers_about_order(order_id))

# === Регистрация водителя (FSM) ===
@bot.on.message(text="🚗 Я водитель")
async def driver_menu_handler(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы и не можете использовать этого бота.")
        return
    
    await bot.state_dispenser.delete(message.from_id)
    
    is_registered = await is_driver_registered(message.from_id)
    is_verified = await is_driver_verified(message.from_id)
    has_sub = await has_active_sub(message.from_id)
    
    if not is_registered:
        await bot.state_dispenser.set(message.from_id, DriverStates.phone, {})
        await message.answer(
            "📱 Для регистрации водителем введите ваш номер телефона в формате +79991234567:",
            keyboard=kb_cancel()
        )
        return
    
    if not is_verified:
        await message.answer(
            "⏳ Ваш профиль водителя ожидает верификации администратором.",
            keyboard=kb_driver_menu()
        )
        return
    
    if not has_sub:
        await message.answer(
            "⚠️ У вас нет активной подписки. Приобретите подписку в разделе '💳 Абонемент'.",
            keyboard=kb_driver_menu()
        )
        return
    
    await message.answer("Выберите действие:", keyboard=kb_driver_menu())

@bot.on.message(state=DriverStates.phone)
async def driver_phone(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Регистрация отменена.", keyboard=kb_main(message.from_id))
        return
    
    if not is_valid_phone(message.text):
        await message.answer("❌ Некорректный номер. Введите в формате +79991234567:")
        return
    
    data = {"phone": message.text.strip()}
    await bot.state_dispenser.set(message.from_id, DriverStates.car_model, data)
    await message.answer("🚗 Введите марку и модель автомобиля:")

@bot.on.message(state=DriverStates.car_model)
async def driver_car_model(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Регистрация отменена.", keyboard=kb_main(message.from_id))
        return
    
    if len(message.text.strip()) < 2:
        await message.answer("❌ Слишком короткое название. Введите марку и модель:")
        return
    
    data = await bot.state_dispenser.get(message.from_id) or {}
    data['car_model'] = message.text.strip()
    await bot.state_dispenser.set(message.from_id, DriverStates.car_year, data)
    await message.answer("📅 Введите год выпуска автомобиля:")

@bot.on.message(state=DriverStates.car_year)
async def driver_car_year(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Регистрация отменена.", keyboard=kb_main(message.from_id))
        return
    
    try:
        year = int(message.text)
        if year < 1990 or year > 2026:
            raise ValueError
    except:
        await message.answer("❌ Введите год от 1990 до 2026:")
        return
    
    data = await bot.state_dispenser.get(message.from_id) or {}
    data['car_year'] = year
    await bot.state_dispenser.set(message.from_id, DriverStates.car_number, data)
    await message.answer("🔢 Введите госномер автомобиля в формате А123БВ178:")

@bot.on.message(state=DriverStates.car_number)
async def driver_car_number(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Регистрация отменена.", keyboard=kb_main(message.from_id))
        return
    
    if not is_valid_car_number(message.text):
        await message.answer("❌ Некорректный формат. Введите номер в формате А123БВ178:")
        return
    
    data = await bot.state_dispenser.get(message.from_id) or {}
    data['car_number'] = message.text.upper()
    
    kb = Keyboard(inline=False)
    kb.add(Text("Стандарт"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("Комфорт"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("Комфорт+"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("Минивэн"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("Бизнес"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    
    await bot.state_dispenser.set(message.from_id, DriverStates.car_class, data)
    await message.answer("🚘 Выберите класс вашего автомобиля:", keyboard=kb.get_json())

@bot.on.message(state=DriverStates.car_class)
async def driver_car_class(message: Message):
    if message.text == "❌ Отменить":
        await bot.state_dispenser.delete(message.from_id)
        await message.answer("Регистрация отменена.", keyboard=kb_main(message.from_id))
        return
    
    class_map = {
        "стандарт": "standard",
        "комфорт": "comfort",
        "комфорт+": "comfort+",
        "минивэн": "minivan",
        "бизнес": "business"
    }
    
    car_class = class_map.get(message.text.lower())
    if not car_class:
        await message.answer("❌ Выберите класс из списка на клавиатуре:")
        return
    
    data = await bot.state_dispenser.get(message.from_id) or {}
    
    async with _pg_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO drivers(user_id, phone, car_model, car_year, car_number, car_class)
               VALUES($1,$2,$3,$4,$5,$6)
               ON CONFLICT(user_id) DO UPDATE SET
               phone=$2, car_model=$3, car_year=$4, car_number=$5, car_class=$6, docs_verified=FALSE""",
            message.from_id, data['phone'], data['car_model'],
            data['car_year'], data['car_number'], car_class
        )
    
    await bot.state_dispenser.delete(message.from_id)
    
    for admin_id in ADMIN_IDS:
        kb = Keyboard(inline=True)
        kb.add(
            Text("✅ Верифицировать", payload={"cmd": "verify_driver", "user_id": message.from_id}),
            color=KeyboardButtonColor.POSITIVE
        ).row()
        kb.add(
            Text("❌ Отклонить", payload={"cmd": "reject_driver", "user_id": message.from_id}),
            color=KeyboardButtonColor.NEGATIVE
        )
        
        admin_text = (
            f"🔔 Новый водитель на проверку:\n"
            f"ID: {message.from_id}\n"
            f"Телефон: {data['phone']}\n"
            f"Авто: {data['car_model']} ({data['car_year']})\n"
            f"Номер: {data['car_number']}\n"
            f"Класс: {car_class}"
        )
        await safe_send(admin_id, admin_text, keyboard=kb.get_json())
    
    await message.answer(
        "✅ Регистрация завершена! Ваша заявка отправлена на проверку администратору.\n"
        "После верификации вы сможете принимать заказы.",
        keyboard=kb_driver_menu()
    )

# === Мои заказы и поездки ===
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
                kb.add(
                    Text(f"{i}⭐", payload={"cmd": "rate_order", "order_id": o['id'], "stars": i})
                )
                if i < 5:
                    kb.row()
        
        await message.answer(text, keyboard=kb.get_json() if kb else None)

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
        await message.answer("У вас пока нет поездок.", keyboard=kb_driver_menu())
        return
    
    for o in orders:
        text = fmt_order(o)
        kb = None
        
        if o['status'] == 'taken':
            kb = Keyboard(inline=True)
            kb.add(
                Text("❌ Отказаться от заказа", payload={"cmd": "driver_cancel", "order_id": o['id']}),
                color=KeyboardButtonColor.NEGATIVE
            )
        
        await message.answer(text, keyboard=kb.get_json() if kb else None)

# === Тарифы ===
@bot.on.message(text="📊 Тарифы")
async def show_tariffs(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    
    text = "📊 Тарифы на поездки:\n\n"
    for key, tariff in TARIFFS.items():
        text += (
            f"🚘 {tariff['name']}:\n"
            f"  • {tariff['price_per_km']} ₽/км\n"
            f"  • Минимальная цена: {tariff['min_price']} ₽\n\n"
        )
    
    text += "💡 Цена зависит от расстояния. Расстояние рассчитывается с коэффициентом 1.25."
    
    await message.answer(text, keyboard=kb_main(message.from_id))

# === Абонемент ===
@bot.on.message(text="💳 Абонемент")
async def subscription_handler(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    
    has_sub = await has_active_sub(message.from_id)
    
    if has_sub:
        async with _pg_pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT expires_date FROM subscriptions WHERE user_id=$1",
                message.from_id
            )
        if r:
            text = (
                f"💳 У вас есть активная подписка!\n"
                f"Действует до: {r['expires_date']}\n\n"
                f"Вы можете продлить подписку, выбрав тариф:"
            )
        else:
            text = "💳 Выберите подписку для водителей:\n\n"
    else:
        text = "💳 Выберите подписку для водителей:\n\n"
    
    kb = Keyboard(inline=False)
    for key, plan in SUBS.items():
        kb.add(
            Text(plan['label'], payload={"cmd": "buy_sub", "plan_key": key}),
            color=KeyboardButtonColor.PRIMARY
        )
        kb.row()
    kb.add(Text("🔙 Назад"), color=KeyboardButtonColor.NEGATIVE)
    
    await message.answer(text, keyboard=kb.get_json())

# === Админ панель ===
@bot.on.message(text="🔧 Админ")
async def admin_panel(message: Message):
    if message.from_id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещён.")
        return
    
    await message.answer("🔧 Административная панель:", keyboard=kb_admin_menu())

@bot.on.message(text="📋 Заказы")
async def admin_orders(message: Message):
    if message.from_id not in ADMIN_IDS:
        return
    
    async with _pg_pool.acquire() as conn:
        orders = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC LIMIT 20")
    
    if not orders:
        await message.answer("Заказов нет.")
        return
    
    for o in orders:
        text = fmt_order(o)
        text += f"\n👤 Пассажир ID: {o['passenger_id']}"
        if o['driver_id']:
            text += f"\n🚗 Водитель ID: {o['driver_id']}"
        text += f"\n📅 Создан: {o['created_at']}"
        
        kb = Keyboard(inline=True)
        if o['status'] == 'open':
            kb.add(
                Text("Отменить", payload={"cmd": "adm_cancel_order", "order_id": o['id']}),
                color=KeyboardButtonColor.NEGATIVE
            )
        elif o['status'] == 'taken':
            kb.add(
                Text("Завершить", payload={"cmd": "adm_complete_order", "order_id": o['id']}),
                color=KeyboardButtonColor.POSITIVE
            )
            kb.add(
                Text("Отменить", payload={"cmd": "adm_cancel_order", "order_id": o['id']}),
                color=KeyboardButtonColor.NEGATIVE
            )
        
        await message.answer(text, keyboard=kb.get_json())

@bot.on.message(text="👥 Пользователи")
async def admin_users(message: Message):
    if message.from_id not in ADMIN_IDS:
        return
    
    async with _pg_pool.acquire() as conn:
        passengers = await conn.fetch(
            "SELECT DISTINCT passenger_id, COUNT(*) as order_count FROM orders GROUP BY passenger_id ORDER BY order_count DESC LIMIT 10"
        )
        drivers = await conn.fetch(
            "SELECT d.*, COUNT(o.id) as order_count FROM drivers d LEFT JOIN orders o ON d.user_id = o.driver_id GROUP BY d.user_id ORDER BY order_count DESC LIMIT 10"
        )
        blacklisted = await conn.fetch("SELECT * FROM blacklist LIMIT 10")
    
    text = "👥 Статистика пользователей:\n\n"
    
    text += "📦 Пассажиры:\n"
    for p in passengers:
        text += f"ID: {p['passenger_id']} | Заказов: {p['order_count']}\n"
    
    text += "\n🚗 Водители:\n"
    for d in drivers:
        status = "✅" if d['docs_verified'] else "⏳"
        text += f"ID: {d['user_id']} {status} | {d['car_model']} | {d['car_class']} | Поездок: {d.get('order_count', 0)}\n"
    
    text += "\n🚫 Чёрный список:\n"
    if blacklisted:
        for b in blacklisted:
            text += f"ID: {b['user_id']} | Причина: {b.get('reason', '—')}\n"
    else:
        text += "Пусто\n"
    
    await message.answer(text)

@bot.on.message(text="✅ Верификация")
async def admin_verification(message: Message):
    if message.from_id not in ADMIN_IDS:
        return
    
    async with _pg_pool.acquire() as conn:
        unverified = await conn.fetch(
            "SELECT * FROM drivers WHERE docs_verified=FALSE LIMIT 10"
        )
    
    if not unverified:
        await message.answer("Нет водителей на верификацию.")
        return
    
    for d in unverified:
        text = (
            f"🚗 Водитель ID: {d['user_id']}\n"
            f"📱 Телефон: {d['phone']}\n"
            f"🚘 Авто: {d['car_model']} ({d['car_year']})\n"
            f"🔢 Номер: {d['car_number']}\n"
            f"🚘 Класс: {d['car_class']}\n"
            f"📅 Зарегистрирован: {d['registered_at']}"
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
    
    text = "💳 Управление подписками:\n\n"
    
    if pending:
        text += "⏳ Ожидают активации:\n"
        for p in pending:
            plan = SUBS.get(p['plan_key'], {})
            text += f"ID: {p['user_id']} | Тариф: {plan.get('label', p['plan_key'])}\n"
    else:
        text += "⏳ Нет ожидающих активации.\n"
    
    if active:
        text += "\n✅ Активные подписки:\n"
        for a in active:
            text += f"ID: {a['user_id']} | До: {a['expires_date']}\n"
    else:
        text += "\n✅ Нет активных подписок.\n"
    
    await message.answer(text)

@bot.on.message(text="📊 Статистика")
async def admin_stats(message: Message):
    if message.from_id not in ADMIN_IDS:
        return
    
    async with _pg_pool.acquire() as conn:
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
        completed_orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status='completed'")
        cancelled_orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status='cancelled'")
        active_orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status IN ('open', 'taken')")
        total_drivers = await conn.fetchval("SELECT COUNT(*) FROM drivers")
        verified_drivers = await conn.fetchval("SELECT COUNT(*) FROM drivers WHERE docs_verified=TRUE")
        total_passengers = await conn.fetchval("SELECT COUNT(DISTINCT passenger_id) FROM orders")
        active_subs = await conn.fetchval(
            "SELECT COUNT(*) FROM subscriptions WHERE expires_date >= $1",
            datetime.now(TZ).date()
        )
        total_revenue = await conn.fetchval("SELECT COALESCE(SUM(price), 0) FROM orders WHERE status='completed'")
    
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

# === Единый обработчик payload ===
@bot.on.message(func=lambda m: m.payload and isinstance(m.payload, dict) and "cmd" in m.payload)
async def unified_payload_handler(message: Message):
    try:
        data = message.payload
        cmd = data.get("cmd")
        uid = message.from_id

        if await check_blacklist(uid) and uid not in ADMIN_IDS:
            await message.answer("⛔ Вы заблокированы и не можете использовать этого бота.")
            return

        # Пользовательские команды
        if cmd == "take_order":
            order_id = data.get("order_id")
            
            if not await is_driver_verified(uid):
                return await message.answer("❌ Ваш профиль не верифицирован.")
            if not await has_active_sub(uid):
                return await message.answer("❌ Нет активной подписки.")
            if await is_driver_busy(uid):
                return await message.answer("❌ У вас уже есть активный заказ. Завершите его перед тем как брать новый.")
            
            async with _pg_pool.acquire() as conn:
                driver_data = await conn.fetchrow(
                    "SELECT car_class FROM drivers WHERE user_id=$1", uid
                )
            
            if not driver_data:
                return await message.answer("❌ Профиль водителя не найден.")
            
            # Атомарно пытаемся взять заказ
            success, error_msg, order = await try_take_order_atomic(order_id, uid)
            
            if not success:
                return await message.answer(f"❌ {error_msg}")
            
            allowed, reason = can_take_order(driver_data['car_class'], order['car_class'])
            if not allowed:
                # Возвращаем заказ в open
                await update_order_status(order_id, 'open')
                return await message.answer(f"❌ {reason}")
            
            await message.answer(f"✅ Вы взяли заказ #{order_id}.\n\n{fmt_order(order)}")
            await safe_send(
                order['passenger_id'],
                f"🚗 Водитель принял ваш заказ #{order_id}. Ожидайте связи с водителем."
            )
            return

        elif cmd == "skip_order":
            await message.answer("Вы пропустили этот заказ.")
            return

        elif cmd == "cancel_order":
            order_id = data.get("order_id")
            order = await get_order(order_id)
            
            if not order or order['passenger_id'] != uid:
                return await message.answer("❌ Ошибка: этот заказ не ваш.")
            if order['status'] not in ['open', 'taken']:
                return await message.answer("❌ Заказ нельзя отменить.")
            
            async with _pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE orders SET status='cancelled', updated_at=NOW() WHERE id=$1",
                    order_id
                )
            
            await message.answer(f"❌ Заказ #{order_id} отменён.")
            
            if order['driver_id']:
                await safe_send(
                    order['driver_id'],
                    f"❌ Пассажир отменил заказ #{order_id}."
                )
            return

        elif cmd == "driver_cancel":
            order_id = data.get("order_id")
            order = await get_order(order_id)
            
            if not order or order['driver_id'] != uid:
                return await message.answer("❌ Ошибка: вы не являетесь водителем этого заказа.")
            if order['status'] != 'taken':
                return await message.answer("❌ Заказ нельзя отменить.")
            
            async with _pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE orders SET status='open', driver_id=NULL, updated_at=NOW() WHERE id=$1",
                    order_id
                )
            
            await message.answer(f"Вы отказались от заказа #{order_id}.")
            await safe_send(
                order['passenger_id'],
                f"⚠️ Водитель отказался от заказа #{order_id}. Заказ снова открыт."
            )
            
            asyncio.create_task(notify_drivers_about_order(order_id))
            return

        elif cmd == "rate_order":
            order_id = data.get("order_id")
            stars = int(data.get("stars", 0))
            order = await get_order(order_id)
            
            if not order or order['passenger_id'] != uid:
                return await message.answer("❌ Ошибка: этот заказ не ваш.")
            if order['status'] != 'completed':
                return await message.answer("❌ Поездка еще не завершена.")
            if stars < 1 or stars > 5:
                return await message.answer("❌ Оценка должна быть от 1 до 5.")
            
            await add_rating(order_id, order['driver_id'], uid, stars)
            avg, cnt = await avg_rating(order['driver_id'])
            
            await message.answer(
                f"⭐ Спасибо за оценку!\n"
                f"Рейтинг водителя: {avg}/5 ({cnt} оценок)"
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
                    """INSERT INTO pending_subscriptions(user_id, plan_key)
                       VALUES($1,$2)
                       ON CONFLICT(user_id) DO UPDATE SET plan_key=$2, created_at=NOW()""",
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
            
            await message.answer(
                f"✅ Заявка на подписку '{plan['label']}' отправлена администратору.\n"
                f"Вы получите уведомление после активации."
            )
            return

        # Админские команды
        if uid in ADMIN_IDS:
            if cmd == "activate_sub":
                target_uid = data.get("user_id")
                plan_key = data.get("plan_key")
                plan = SUBS.get(plan_key)
                
                if not plan or not target_uid:
                    return await message.answer("❌ Ошибка данных.")
                
                async with _pg_pool.acquire() as conn:
                    rec = await conn.fetchrow(
                        "SELECT expires_date FROM subscriptions WHERE user_id=$1",
                        target_uid
                    )
                    base_date = datetime.now(TZ).date()
                    
                    if rec and rec['expires_date']:
                        try:
                            if isinstance(rec['expires_date'], str):
                                old_date = datetime.strptime(rec['expires_date'], "%Y-%m-%d").date()
                            else:
                                old_date = rec['expires_date']
                            if old_date > base_date:
                                base_date = old_date
                        except:
                            pass
                    
                    new_exp = base_date + timedelta(days=plan['days'])
                    
                    await conn.execute(
                        """INSERT INTO subscriptions(user_id, expires_date)
                           VALUES ($1, $2)
                           ON CONFLICT(user_id) DO UPDATE SET expires_date=EXCLUDED.expires_date""",
                        target_uid, new_exp
                    )
                    await conn.execute(
                        "DELETE FROM pending_subscriptions WHERE user_id=$1",
                        target_uid
                    )
                    await conn.execute(
                        """INSERT INTO subscription_log(user_id, target_user_id, plan_key, admin_id, action)
                           VALUES ($1, $2, $3, $4, 'activate')""",
                        target_uid, target_uid, plan_key, uid
                    )
                
                await safe_send(
                    target_uid,
                    f"🎉 Ваша подписка активирована до {new_exp.strftime('%d.%m.%Y')}!"
                )
                await message.answer(
                    f"✅ Подписка для пользователя {target_uid} активирована до {new_exp.strftime('%d.%m.%Y')}"
                )
                return

            elif cmd == "reject_sub":
                target_uid = data.get("user_id")
                
                async with _pg_pool.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM pending_subscriptions WHERE user_id=$1",
                        target_uid
                    )
                    await conn.execute(
                        """INSERT INTO subscription_log(user_id, target_user_id, admin_id, action)
                           VALUES ($1, $2, $3, 'reject')""",
                        target_uid, target_uid, uid
                    )
                
                await safe_send(target_uid, "❌ Ваша заявка на подписку отклонена администратором.")
                await message.answer(f"Заявка на подписку от {target_uid} отклонена.")
                return

            elif cmd == "verify_driver":
                target_uid = data.get("user_id")
                
                async with _pg_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE drivers SET docs_verified=TRUE WHERE user_id=$1",
                        target_uid
                    )
                
                await safe_send(
                    target_uid,
                    "✅ Ваш профиль водителя верифицирован! Теперь вы можете принимать заказы."
                )
                await message.answer(f"✅ Водитель {target_uid} верифицирован.")
                return

            elif cmd == "reject_driver":
                target_uid = data.get("user_id")
                
                async with _pg_pool.acquire() as conn:
                    # Сбрасываем верификацию вместо удаления
                    await conn.execute(
                        "UPDATE drivers SET docs_verified=FALSE WHERE user_id=$1",
                        target_uid
                    )
                
                await safe_send(
                    target_uid,
                    "❌ Ваша заявка на регистрацию водителя отклонена администратором. "
                    "Вы можете повторно подать заявку."
                )
                await message.answer(f"Заявка водителя {target_uid} отклонена.")
                return

            elif cmd == "adm_cancel_order":
                order_id = data.get("order_id")
                order = await get_order(order_id)
                
                if not order:
                    return await message.answer("Заказ не найден.")
                
                await update_order_status(order_id, "cancelled")
                await message.answer(f"❌ Заказ #{order_id} отменён.")
                
                if order['passenger_id']:
                    await safe_send(
                        order['passenger_id'],
                        f"❌ Ваш заказ #{order_id} отменён администратором."
                    )
                if order['driver_id']:
                    await safe_send(
                        order['driver_id'],
                        f"❌ Заказ #{order_id} отменён администратором."
                    )
                return

            elif cmd == "adm_complete_order":
                order_id = data.get("order_id")
                order = await get_order(order_id)
                
                if not order:
                    return await message.answer("Заказ не найден.")
                
                await update_order_status(order_id, "completed")
                await message.answer(f"✅ Заказ #{order_id} завершён.")
                
                if order['passenger_id']:
                    kb = Keyboard(inline=True)
                    for i in range(1, 6):
                        kb.add(
                            Text(f"{i}⭐", payload={"cmd": "rate_order", "order_id": order_id, "stars": i})
                        )
                        if i < 5:
                            kb.row()
                    
                    await safe_send(
                        order['passenger_id'],
                        f"✅ Ваш заказ #{order_id} завершён.\n\nПожалуйста, оцените поездку:",
                        keyboard=kb.get_json()
                    )
                if order['driver_id']:
                    await safe_send(
                        order['driver_id'],
                        f"✅ Заказ #{order_id} завершён администратором."
                    )
                return

    except Exception as e:
        log.error(f"Ошибка в обработчике payload: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка. Попробуйте позже.")

# === Команды бана/разбана ===
@bot.on.message(text=["/ban"])
async def admin_ban(message: Message):
    if message.from_id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещён.")
        return
    
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /ban ID [причина]")
        return
    
    target_id = int(parts[1])
    if target_id <= 0 or target_id > 2**63 - 1:
        await message.answer("Неверный ID пользователя.")
        return
    
    reason = " ".join(parts[2:]) if len(parts) > 2 else "Не указана"
    
    async with _pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO blacklist(user_id, reason) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            target_id, reason
        )
    
    await message.answer(f"🚫 Пользователь {target_id} добавлен в черный список.")
    
    try:
        await safe_send(
            target_id,
            f"⛔ Вы были заблокированы администратором.\nПричина: {reason}"
        )
    except:
        pass

@bot.on.message(text=["/unban"])
async def admin_unban(message: Message):
    if message.from_id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещён.")
        return
    
    parts = message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /unban ID")
        return
    
    target_id = int(parts[1])
    if target_id <= 0 or target_id > 2**63 - 1:
        await message.answer("Неверный ID пользователя.")
        return
    
    async with _pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM blacklist WHERE user_id=$1", target_id)
    
    await message.answer(f"✅ Пользователь {target_id} удалён из черного списка.")
    
    try:
        await safe_send(target_id, "✅ Ваша блокировка снята администратором.")
    except:
        pass

# === Профиль водителя ===
@bot.on.message(text="👤 Мой профиль")
async def driver_profile(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    
    if not await is_driver_registered(message.from_id):
        await message.answer("Вы не зарегистрированы как водитель.", keyboard=kb_main(message.from_id))
        return
    
    async with _pg_pool.acquire() as conn:
        driver = await conn.fetchrow(
            "SELECT * FROM drivers WHERE user_id=$1",
            message.from_id
        )
    
    if not driver:
        await message.answer("Профиль не найден.", keyboard=kb_main(message.from_id))
        return
    
    is_verified = driver['docs_verified']
    has_sub = await has_active_sub(message.from_id)
    avg, cnt = await avg_rating(message.from_id)
    is_busy = await is_driver_busy(message.from_id)
    
    text = (
        f"👤 Профиль водителя:\n\n"
        f"📱 Телефон: {driver['phone']}\n"
        f"🚗 Автомобиль: {driver['car_model']} ({driver['car_year']})\n"
        f"🔢 Номер: {driver['car_number']}\n"
        f"🚘 Класс: {driver['car_class']}\n"
        f"✅ Верификация: {'Да' if is_verified else 'Нет'}\n"
        f"💳 Подписка: {'Активна' if has_sub else 'Отсутствует'}\n"
        f"📊 Статус: {'Занят' if is_busy else 'Свободен'}\n"
        f"⭐ Рейтинг: {avg}/5 ({cnt} оценок)"
    )
    
    await message.answer(text, keyboard=kb_driver_menu())

@bot.on.message(text="📊 Моя статистика")
async def driver_stats(message: Message):
    if await check_blacklist(message.from_id):
        await message.answer("⛔ Вы заблокированы.")
        return
    
    if not await is_driver_registered(message.from_id):
        await message.answer("Вы не зарегистрированы как водитель.", keyboard=kb_main(message.from_id))
        return
    
    async with _pg_pool.acquire() as conn:
        completed = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE driver_id=$1 AND status='completed'",
            message.from_id
        )
        cancelled = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE driver_id=$1 AND status='cancelled'",
            message.from_id
        )
        total_earned = await conn.fetchval(
            "SELECT COALESCE(SUM(price), 0) FROM orders WHERE driver_id=$1 AND status='completed'",
            message.from_id
        )
    
    avg, cnt = await avg_rating(message.from_id)
    
    text = (
        f"📊 Статистика водителя:\n\n"
        f"✅ Завершённых поездок: {completed}\n"
        f"❌ Отменённых: {cancelled}\n"
        f"💰 Заработано: {total_earned} ₽\n"
        f"⭐ Средний рейтинг: {avg}/5\n"
        f"📝 Всего оценок: {cnt}"
    )
    
    await message.answer(text, keyboard=kb_driver_menu())

# === Запуск ===
async def main():
    await init_pg()
    log.info("🚀 Бот Межгород Трансфер Россия ВК успешно запущен!")
    await bot.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бот остановлен пользователем")
