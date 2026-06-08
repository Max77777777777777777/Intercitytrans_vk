# ══════════════════════════════════════════════════════════════
#  МЕЖГОРОД ТРАНСФЕР РОССИЯ — vkbottle (ВКонтакте)
# ══════════════════════════════════════════════════════════════
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
import html

import asyncpg
from geopy.geocoders import Yandex
from geopy.distance import geodesic as geo_dist

from vkbottle.bot import Bot, Message, BaseStateGroup
from vkbottle import Keyboard, KeyboardButtonColor, Text

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

# === Инициализация базы и таблиц ===
async def init_pg():
    global _pg_pool
    _pg_pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=10)
    async with _pg_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS orders(
                id SERIAL PRIMARY KEY,
                passenger_id BIGINT,
                from_city TEXT, to_city TEXT,
                trip_date TEXT, trip_time TEXT,
                passengers INT, car_class TEXT,
                wishes TEXT, distance_km REAL,
                price INT, status TEXT DEFAULT 'open',
                driver_id BIGINT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS drivers(
                user_id BIGINT PRIMARY KEY,
                phone TEXT,
                car_model TEXT,
                car_year INT,
                car_number TEXT,
                car_class TEXT,
                docs_verified BOOL DEFAULT FALSE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions(
                user_id BIGINT PRIMARY KEY,
                expires_date TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_subscriptions(
                user_id BIGINT PRIMARY KEY,
                plan_key TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_log(
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                plan_key TEXT,
                admin_id BIGINT,
                action TEXT,
                created_at TEXT
            )
        """)

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

# === Утилиты ===
def esc(text):
    return html.escape(text) if text else "—"

def is_valid_city(city):
    return bool(city) and len(city.strip()) >= 2

def is_valid_date(text):
    try:
        d = datetime.strptime(text, "%d.%m.%Y")
        return d.date() >= datetime.now(TZ).date()
    except:
        return False

def is_valid_time(text):
    return bool(re.match(r"^\d{1,2}:\d{2}$", text))

def geocode(city):
    if not geolocator or not city:
        return None
    try:
        loc = geolocator.geocode(city, timeout=5)
        if loc:
            return loc.latitude, loc.longitude
    except Exception as ex:
        log.error(f"Geocode error for '{city}': {ex}")
    return None

def calc_distance(city_from, city_to):
    c1 = geocode(city_from)
    c2 = geocode(city_to)
    if c1 and c2:
        return geo_dist(c1, c2).km
    return None

def calc_price(dist_km, car_class):
    prices = {'standard': 25, 'comfort': 34, 'comfort+': 40, 'minivan': 45, 'business': 60}
    price_per_km = prices.get(car_class, 25)
    return int(dist_km * price_per_km * DIST_COEFF)

def fmt_order(o):
    lines = [
        f"🚕 Заказ #{o['id']}",
        f"📍 {esc(o['from_city'])} → {esc(o['to_city'])}",
        f"📅 {o['trip_date']} 🕐 {o['trip_time']}",
        f"👥 Пассажиров: {o['passengers']}",
        f"🚘 Класс: {o['car_class']}",
        f"💬 Пожелания: {esc(o.get('wishes',''))}",
        f"Статус: {o['status']}",
    ]
    return "\n".join(lines)

async def safe_send(user_id, text, keyboard=None):
    try:
        await bot.api.messages.send(peer_id=user_id, message=text, keyboard=keyboard, random_id=0)
    except Exception as e:
        log.error(f"Ошибка отправки {user_id}: {e}")

async def notify_drivers_about_order(order_id):
    async with _pg_pool.acquire() as conn:
        drivers = await conn.fetch("SELECT user_id FROM drivers WHERE docs_verified=TRUE")
        order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
        if not order:
            return
        
    text_notify = f"Новый заказ #{order_id}:\n{fmt_order(order)}"
    kb = Keyboard(inline=True)
    kb.add(Text("✅ Взять заказ", payload={"cmd": "take", "order_id": order_id}), color=KeyboardButtonColor.POSITIVE).row()
    kb.add(Text("➡️ Пропустить", payload={"cmd": "skip", "order_id": order_id}), color=KeyboardButtonColor.SECONDARY)
    kjson = kb.get_json()
    
    batch_size = 20
    delay = 1.1  
    
    for i in range(0, len(drivers), batch_size):
        batch = drivers[i:i+batch_size]
        send_tasks = [safe_send(d['user_id'], text_notify, keyboard=kjson) for d in batch]
        await asyncio.gather(*send_tasks)
        if i + batch_size < len(drivers):
            await asyncio.sleep(delay)

async def set_driver_verified(user_id: int, verified: bool):
    async with _pg_pool.acquire() as conn:
        await conn.execute("UPDATE drivers SET docs_verified=$1 WHERE user_id=$2", verified, user_id)

# === Клавиатуры ===
def kb_main():
    kb = Keyboard(inline=False)
    kb.add(Text("🚕 Создать заказ"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("🚗 Я водитель"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("📋 Мои заказы"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("📊 Тарифы"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("💳 Абонемент"), color=KeyboardButtonColor.SECONDARY)
    if os.getenv("ADMIN_IDS") and str(os.getenv("ADMIN_IDS")):
        kb.add(Text("🔧 Админ"), color=KeyboardButtonColor.NEGATIVE) 
    return kb.get_json()

def kb_cancel():
    kb = Keyboard(inline=False)
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()

def kb_subs():
    kb = Keyboard(inline=True)
    for k, p in SUBS.items():
        kb.add(Text(p['label'], payload={"sub": k}))
        kb.row()
    kb.add(Text("Отмена"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


# === Общие Хэндлеры и Навигация ===
@bot.on.message(text=["🔙 Главное меню", "/start", "Начать", "Отмена"])
async def main_menu(message: Message):
    await bot.state_dispenser.delete(message.peer_id)
    await message.answer("🏠 Главное меню", keyboard=kb_main())

@bot.on.message(text="❌ Отменить")
async def cancel_handler(message: Message):
    await bot.state_dispenser.delete(message.peer_id)
    await message.answer("Действие отменено.", keyboard=kb_main())


# === FSM: Создание Заказа (Пассажир) ===
@bot.on.message(text="🚕 Создать заказ")
async def start_order(message: Message):
    await bot.state_dispenser.set(message.peer_id, OrderStates.from_city)
    await message.answer("Введите город отправления:", keyboard=kb_cancel())

@bot.on.message(state=OrderStates.from_city)
async def from_city_step(message: Message):
    city = message.text.strip()
    if city == "❌ Отменить": return
    if not is_valid_city(city):
        await message.answer("Неверное название города. Попробуйте ещё раз.")
        return
    await bot.state_dispenser.set(message.peer_id, OrderStates.to_city, from_city=city)
    await message.answer(f"Откуда: {esc(city)}\nВведите город назначения:")

@bot.on.message(state=OrderStates.to_city)
async def to_city_step(message: Message):
    city = message.text.strip()
    if city == "❌ Отменить": return
    ctx = await bot.state_dispenser.get(message.peer_id)
    if city.lower() == ctx.payload.get("from_city", "").lower():
        await message.answer("Город назначения не должен совпадать с городом отправления. Введите другой город.")
        return
    await bot.state_dispenser.set(message.peer_id, OrderStates.trip_date, **ctx.payload, to_city=city)
    await message.answer(f"Куда: {esc(city)}\nВведите дату поездки (ДД.ММ.ГГГГ):")

@bot.on.message(state=OrderStates.trip_date)
async def trip_date_step(message: Message):
    if message.text.strip() == "❌ Отменить": return
    if not is_valid_date(message.text.strip()):
        await message.answer("Неверный формат даты или дата в прошлом. Введите заново (ДД.ММ.ГГГГ):")
        return
    ctx = await bot.state_dispenser.get(message.peer_id)
    await bot.state_dispenser.set(message.peer_id, OrderStates.trip_time, **ctx.payload, trip_date=message.text.strip())
    await message.answer("Введите время поездки (ЧЧ:ММ):")

@bot.on.message(state=OrderStates.trip_time)
async def trip_time_step(message: Message):
    if message.text.strip() == "❌ Отменить": return
    if not is_valid_time(message.text.strip()):
        await message.answer("Неверный формат времени. Введите в формате ЧЧ:ММ")
        return
    ctx = await bot.state_dispenser.get(message.peer_id)
    await bot.state_dispenser.set(message.peer_id, OrderStates.passengers, **ctx.payload, trip_time=message.text.strip())
    await message.answer("Сколько пассажиров? Введите число:")

@bot.on.message(state=OrderStates.passengers)
async def passengers_step(message: Message):
    if message.text.strip() == "❌ Отменить": return
    if not message.text.isdigit() or not (1 <= int(message.text) <= 8):
        await message.answer("Введите число от 1 до 8.")
        return
    ctx = await bot.state_dispenser.get(message.peer_id)
    passengers = int(message.text)
    await bot.state_dispenser.set(message.peer_id, OrderStates.car_class, **ctx.payload, passengers=passengers)
    
    kb = Keyboard(inline=True)
    classes = ["standard", "comfort", "comfort+", "minivan", "business"]
    prices = {'standard': 25, 'comfort': 34, 'comfort+': 40, 'minivan': 45, 'business': 60}
    for c in classes:
        kb.add(Text(f"{c.title()} — {prices[c]} ₽/км", payload={"car_class": c})).row()
    kb.add(Text("Отменить", payload={"cancel": True}), color=KeyboardButtonColor.NEGATIVE)
    await message.answer("Выберите класс авто:", keyboard=kb.get_json())

@bot.on.message(state=OrderStates.car_class, payload_contains={"car_class": None})
async def car_class_step(message: Message):
    c = json.loads(message.payload).get("car_class")
    ctx = await bot.state_dispenser.get(message.peer_id)
    await bot.state_dispenser.set(message.peer_id, OrderStates.wishes, **ctx.payload, car_class=c)
    kb = Keyboard(inline=False)
    kb.add(Text("Нет"))
    kb.add(Text("❌ Отменить"), color=KeyboardButtonColor.NEGATIVE)
    await message.answer("Пожелания к поездке? Введите текст или нажмите «Нет».", keyboard=kb.get_json())

@bot.on.message(state=OrderStates.wishes)
async def step_wishes_notify(message: Message):
    text = message.text.strip()
    if text == "❌ Отменить": return
    if text.lower() in ["нет", "-", "—", "no"]:
        text = ""
    ctx = await bot.state_dispenser.get(message.peer_id)
    data = ctx.payload
    
    dist = await asyncio.to_thread(calc_distance, data["from_city"], data["to_city"])
    if not dist:
        await bot.state_dispenser.delete(message.peer_id)
        await message.answer("❌ Не удалось рассчитать расстояние между городами. Проверьте корректность названий.", keyboard=kb_main())
        return
    price = calc_price(dist, data["car_class"])
    
    async with _pg_pool.acquire() as conn:
        res = await conn.fetchrow("""
            INSERT INTO orders(passenger_id, from_city, to_city, trip_date, trip_time, passengers, car_class, wishes, distance_km, price, status) 
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'open') RETURNING *
        """, message.from_id, data['from_city'], data['to_city'], data['trip_date'], data['trip_time'],
           data['passengers'], data['car_class'], text, dist, price)
        order_id = res['id']

    await bot.state_dispenser.delete(message.peer_id)
    await message.answer(f"Ваш заказ #{order_id} создан.\nРасстояние: {dist:.1f} км\nЦена: {price} ₽", keyboard=kb_main())
    await notify_drivers_about_order(order_id)


# === FSM: Регистрация Водителя ===
@bot.on.message(text="🚗 Я водитель")
async def driver_entry(message: Message):
    async with _pg_pool.acquire() as conn:
        drv = await conn.fetchrow("SELECT * FROM drivers WHERE user_id = $1", message.from_id)
    if not drv:
        await bot.state_dispenser.set(message.peer_id, DriverStates.phone)
        await message.answer("Введите номер телефона для регистрации (например, +79001234567):", keyboard=kb_cancel())
    else:
        status = "✅ Профиль верифицирован." if drv['docs_verified'] else "⏳ На верификации у админа."
        await message.answer(f"Вы уже зарегистрированы как водитель.\nСтатус: {status}", keyboard=kb_main())

@bot.on.message(state=DriverStates.phone)
async def reg_phone(message: Message):
    phone = message.text.strip()
    if phone == "❌ Отменить": return
    if not re.match(r"^\+?\d{10,15}$", phone):
        await message.answer("Некорректный номер. Введите номер в формате +79001234567.")
        return
    await bot.state_dispenser.set(message.peer_id, DriverStates.car_model, phone=phone)
    await message.answer("Введите марку и модель авто (например, Skoda Octavia):")

@bot.on.message(state=DriverStates.car_model)
async def reg_car_model(message: Message):
    model = message.text.strip()
    if model == "❌ Отменить": return
    if len(model) < 2:
        await message.answer("Введите корректную марку и модель авто.")
        return
    ctx = await bot.state_dispenser.get(message.peer_id)
    await bot.state_dispenser.set(message.peer_id, DriverStates.car_year, **ctx.payload, car_model=model)
    await message.answer("Введите год выпуска авто:")

@bot.on.message(state=DriverStates.car_year)
async def reg_car_year(message: Message):
    if message.text.strip() == "❌ Отменить": return
    try:
        year = int(message.text.strip())
    except ValueError:
        await message.answer("Введите год числом, например 2018.")
        return
    if year < 2000 or year > datetime.now().year + 1:
        await message.answer("Некорректный год выпуска.")
        return
    ctx = await bot.state_dispenser.get(message.peer_id)
    await bot.state_dispenser.set(message.peer_id, DriverStates.car_number, **ctx.payload, car_year=year)
    await message.answer("Введите гос. номер авто:")

@bot.on.message(state=DriverStates.car_number)
async def reg_car_number(message: Message):
    number = message.text.strip().upper().replace(" ", "")
    if number == "❌ ОЙ, ОТМЕНИТЬ": return
    ctx = await bot.state_dispenser.get(message.peer_id)
    await bot.state_dispenser.set(message.peer_id, DriverStates.car_class, **ctx.payload, car_number=number)
    
    kb = Keyboard(inline=True)
    classes = ["standard", "comfort", "comfort+", "minivan", "business"]
    for c in classes:
        kb.add(Text(f"{c.title()}", payload={"reg_class": c})).row()
    await message.answer("Выберите класс авто:", keyboard=kb.get_json())

@bot.on.message(state=DriverStates.car_class, payload_contains={"reg_class": None})
async def reg_car_class(message: Message):
    c = json.loads(message.payload).get("reg_class")
    ctx = await bot.state_dispenser.get(message.peer_id)
    async with _pg_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO drivers(user_id, phone, car_model, car_year, car_number, car_class, docs_verified)
            VALUES ($1,$2,$3,$4,$5,$6,FALSE)
            ON CONFLICT(user_id) DO UPDATE SET phone=EXCLUDED.phone, car_model=EXCLUDED.car_model,
            car_year=EXCLUDED.car_year, car_number=EXCLUDED.car_number, car_class=EXCLUDED.car_class
        """, message.from_id, ctx.payload["phone"], ctx.payload["car_model"], ctx.payload["car_year"],
           ctx.payload["car_number"], c)
    await bot.state_dispenser.delete(message.peer_id)
    await message.answer("Регистрация завершена! Ожидайте верификации администратором.", keyboard=kb_main())


# === Меню Абонементов и Подписки ===
@bot.on.message(text="💳 Абонемент")
async def subscription_menu(message: Message):
    await message.answer("Выберите тариф:", keyboard=kb_subs())

@bot.on.message(payload_contains={"sub": None})
async def subscription_choose(message: Message):
    pk = json.loads(message.payload)['sub']
    plan = SUBS.get(pk)
    if not plan:
        await message.answer("Ошибка выбора, попробуйте снова.")
        return
    async with _pg_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO pending_subscriptions(user_id, plan_key)
            VALUES ($1, $2)
            ON CONFLICT(user_id) DO UPDATE SET plan_key=EXCLUDED.plan_key
        """, message.from_id, pk)
    
    notify_text = f"💳 Запрос на подписку {plan['label']} от ID {message.from_id}"
    for admin_id in ADMIN_IDS:
        await safe_send(admin_id, notify_text)
    await message.answer("Заявка принята, после оплаты сообщите администратором.", keyboard=kb_main())


# === Панель Администратора ===
@bot.on.message(text="🔧 Админ")
async def admin_panel(message: Message):
    if message.from_id not in ADMIN_IDS:
        await message.answer("Доступ запрещён.")
        return
    kb = Keyboard(inline=False)
    kb.add(Text("👥 Водители"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("💳 Заявки подписок"), color=KeyboardButtonColor.PRIMARY).row()
    kb.add(Text("🔙 Главное меню"), color=KeyboardButtonColor.SECONDARY)
    await message.answer("🔧 Меню администратора:", keyboard=kb.get_json())

@bot.on.message(text="💳 Заявки подписок")
async def admin_pending_subs(message: Message):
    if message.from_id not in ADMIN_IDS: return
    async with _pg_pool.acquire() as conn:
        pending = await conn.fetch("SELECT * FROM pending_subscriptions")
    if not pending:
        await message.answer("Нет ожидающих подтверждения подписок.")
        return
    for p in pending:
        text = f"Запрос на подписку {p['plan_key']} от ID {p['user_id']}"
        kb = Keyboard(inline=True)
        kb.add(Text("✅ Активировать", payload={"cmd": "activate_sub", "user_id": p['user_id'], "plan_key": p['plan_key']}), color=KeyboardButtonColor.POSITIVE).row()
        kb.add(Text("❌ Отклонить", payload={"cmd": "reject_sub", "user_id": p['user_id']}), color=KeyboardButtonColor.NEGATIVE)
        await message.answer(text, keyboard=kb.get_json())

@bot.on.message(text="👥 Водители")
async def admin_drivers_list(message: Message):
    if message.from_id not in ADMIN_IDS: return
    async with _pg_pool.acquire() as conn:
        drivers = await conn.fetch("SELECT * FROM drivers")
    if not drivers:
        await message.answer("Нет зарегистрированных водителей.")
        return
    for d in drivers:
        status = "✅ Верифицирован" if d['docs_verified'] else "❌ Не верифицирован"
        text = (f"👤 ID: {d['user_id']}\n"
                f"Телефон: {esc(d['phone'])}\n"
                f"Авто: {esc(d['car_model'])} ({d['car_year']})\n"
                f"Номер: {esc(d['car_number'])}\n"
                f"Класс: {esc(d['car_class'])}\n"
                f"Статус: {status}")
        kb = Keyboard(inline=True)
        if not d['docs_verified']:
            kb.add(Text("✅ Верифицировать", payload={"cmd": "verify_driver", "user_id": d['user_id']}), color=KeyboardButtonColor.POSITIVE).row()
        kb.add(Text("❌ Отклонить", payload={"cmd": "reject_driver", "user_id": d['user_id']}), color=KeyboardButtonColor.NEGATIVE)
        await message.answer(text, keyboard=kb.get_json())


# === Единый Роутер Callback / Payload Команд Админа ===
@bot.on.message(payload_contains={"cmd": None})
async def admin_payload_router(message: Message):
    if message.from_id not in ADMIN_IDS:
        return await message.answer("Доступ запрещён.")
        
    data = json.loads(message.payload)
    cmd = data.get("cmd")
    uid = data.get("user_id")
    
    # 💳 Команды управления подписками
    if cmd == "activate_sub":
        pk = data.get("plan_key")
        plan = SUBS.get(pk)
        if not plan:
            return await message.answer("Ошибка тарифного плана.")
        async with _pg_pool.acquire() as conn:
            rec = await conn.fetchrow("SELECT expires_date FROM subscriptions WHERE user_id=$1", uid)
            base_date = datetime.now(TZ).date()
            if rec and rec['expires_date']:
                try:
                    old_date = datetime.strptime(rec['expires_date'], "%Y-%m-%d").date()
                    if old_date > base_date:
                        base_date = old_date
                except:
                    pass
            new_exp = base_date + timedelta(days=plan['days'])
            await conn.execute("""
                INSERT INTO subscriptions(user_id, expires_date)
                VALUES ($1, $2)
                ON CONFLICT(user_id) DO UPDATE SET expires_date=EXCLUDED.expires_date
            """, uid, new_exp.strftime("%Y-%m-%d"))
            await conn.execute("DELETE FROM pending_subscriptions WHERE user_id=$1", uid)
            await conn.execute("""
                INSERT INTO subscription_log(user_id, plan_key, admin_id, action, created_at)
                VALUES ($1, $2, $3, $4, $5)
            """, uid, pk, message.from_id, "activate", datetime.now(TZ).isoformat())
        await safe_send(uid, f"🎉 Ваша подписка активирована до {new_exp.strftime('%d.%m.%Y')}!")
        await message.answer(f"Подписка для {uid} активирована до {new_exp.strftime('%d.%m.%Y')}")
        
    elif cmd == "reject_sub":
        async with _pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM pending_subscriptions WHERE user_id=$1", uid)
            await conn.execute("""
                INSERT INTO subscription_log(user_id, admin_id, action, created_at)
                VALUES ($1, $2, $3, $4)
            """, uid, message.from_id, "reject", datetime.now(TZ).isoformat())
        await safe_send(uid, "❌ Ваша заявка на подписку отклонена администратором.")
        await message.answer("Заявка на подписку отклонена.")
        
    # 🚗 Команды верификации водителей
    elif cmd == "verify_driver":
        await set_driver_verified(uid, True)
        await safe_send(uid, "✅ Ваш профиль верифицирован. Теперь вы можете принимать заказы.")
        await message.answer("Водитель успешно верифицирован.")
        
    elif cmd == "reject_driver":
        await set_driver_verified(uid, False)
        await safe_send(uid, "❌ Ваш профиль отклонён администратором.")
        await message.answer("Профиль водителя отклонён.")


# === Точка Входа ===
async def main():
    await init_pg()
    log.info("🚀 Бот Межгород Трансфер Россия (ВК) успешно запущен!")
    await bot.run_forever()

if __name__ == "__main__":
    asyncio.run(main())
