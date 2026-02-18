import asyncio
import re
import sqlite3
from datetime import datetime
from typing import Optional, Dict
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = "Bot_token"
CHECK_INTERVAL = 3600
AD_INTERVAL = 600

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Мои игры"), KeyboardButton(text="🔄 Проверить цены")],
            [KeyboardButton(text="💳 Пополнить Steam"), KeyboardButton(text="❓ Помощь")],
            [KeyboardButton(text="⚙️ Настройки")]
        ],
        resize_keyboard=True
    )

def get_steam_refill_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить Steam со скидкой!", url="https://ggsel.net/catalog/product/3-popolnenie-steam-ua-ru-kz-sng-24-7-podarok-5051848")]
    ])
    return keyboard

def init_db():
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS monitored_games
                 (user_id INTEGER, 
                  app_id INTEGER, 
                  game_name TEXT,
                  initial_price REAL,
                  current_price REAL,
                  discount INTEGER,
                  last_check TEXT,
                  last_notified_price REAL,
                  last_notified_discount INTEGER,
                  PRIMARY KEY (user_id, app_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY,
                  first_seen TEXT,
                  last_active TEXT)''')
    conn.commit()
    conn.close()

def register_user(user_id: int):
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("INSERT OR IGNORE INTO users (user_id, first_seen, last_active) VALUES (?, ?, ?)",
              (user_id, now, now))
    c.execute("UPDATE users SET last_active=? WHERE user_id=?", (now, user_id))
    conn.commit()
    conn.close()

async def extract_appid_from_url(url: str) -> Optional[int]:
    patterns = [r'store\.steampowered\.com/app/(\d+)', r'steamcommunity\.com/app/(\d+)']
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return int(match.group(1))
    return None

async def get_game_info(app_id: int, region: str = 'ru') -> Optional[Dict]:
    url = f"https://store.steampowered.com/api/appdetails/?appids={app_id}&cc={region}&l=russian"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and str(app_id) in data and data[str(app_id)]['success']:
                        game_data = data[str(app_id)]['data']
                        if 'price_overview' not in game_data:
                            return {'name': game_data.get('name', 'Unknown'), 'price': 0, 'discount': 0, 
                                    'final_price': 0, 'currency': 'RUB', 'is_free': game_data.get('is_free', True)}
                        price_info = game_data['price_overview']
                        return {'name': game_data['name'], 'price': price_info['initial'] / 100,
                                'final_price': price_info['final'] / 100, 'discount': price_info['discount_percent'],
                                'currency': price_info['currency'], 'is_free': game_data.get('is_free', False)}
    except:
        pass
    return None

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    register_user(message.from_user.id)
    await message.answer(
        "🎮 <b>Steam Price Monitor Bot</b>\n\nОтслеживайте цены и скидки на игры в Steam!\n\n"
        "📎 <b>Просто отправь ссылку на игру</b> - я добавлю её в мониторинг\n\n"
        "Я буду присылать уведомления:\n• При любом изменении цены\n• Когда появится скидка\n• Когда скидка увеличится\n\n"
        "Используй кнопки меню ниже! 👇", parse_mode="HTML", reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "❓ Помощь")
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    register_user(message.from_user.id)
    await message.answer(
        "📖 <b>Как пользоваться:</b>\n\n1️⃣ Скопируй ссылку на игру из Steam\n2️⃣ Отправь её мне\n"
        "3️⃣ Я добавлю игру в мониторинг\n4️⃣ Получай уведомления о любых изменениях цены!\n\n"
        "<b>Примеры ссылок:</b>\n• https://store.steampowered.com/app/730/\n• https://store.steampowered.com/app/1091500/Cyberpunk_2077/\n\n"
        "<b>Команды:</b>\n/remove [ID] - удалить игру из мониторинга\n\n💡 <i>Проверка цен происходит автоматически каждый час</i>\n"
        "💡 <i>Уведомления приходят при ЛЮБОМ изменении цены или скидки</i>",
        parse_mode="HTML", reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "📊 Мои игры")
@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    register_user(message.from_user.id)
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    c.execute("SELECT app_id, game_name, current_price, discount FROM monitored_games WHERE user_id=?", (message.from_user.id,))
    games = c.fetchall()
    conn.close()
    if not games:
        await message.answer("📭 У вас нет отслеживаемых игр.\n\nОтправьте ссылку на игру из Steam для добавления!",
                           reply_markup=get_main_keyboard())
        return
    text = "📊 <b>Ваши отслеживаемые игры:</b>\n\n"
    for app_id, name, price, discount in games:
        discount_emoji = "🔥" if discount > 0 else "💰"
        text += f"{discount_emoji} <b>{name}</b>\n   ID: <code>{app_id}</code>\n   Цена: {price:.2f} ₽"
        if discount > 0:
            text += f" (-{discount}% скидка!)"
        text += f"\n\n"
    text += f"<i>Всего игр: {len(games)}</i>"
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(Command("remove"))
async def cmd_remove(message: types.Message):
    register_user(message.from_user.id)
    try:
        app_id = int(message.text.split()[1])
        conn = sqlite3.connect('steam_monitor.db')
        c = conn.cursor()
        c.execute("DELETE FROM monitored_games WHERE user_id=? AND app_id=?", (message.from_user.id, app_id))
        conn.commit()
        if c.rowcount > 0:
            await message.answer("✅ Игра удалена из мониторинга!", reply_markup=get_main_keyboard())
        else:
            await message.answer("❌ Игра не найдена в вашем списке", reply_markup=get_main_keyboard())
        conn.close()
    except:
        await message.answer("❌ Использование: /remove [ID игры]\nПример: /remove 730\n\nID игры можно узнать в списке (📊 Мои игры)",
                           reply_markup=get_main_keyboard())

@dp.message(F.text == "🔄 Проверить цены")
@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    register_user(message.from_user.id)
    await message.answer("🔄 Проверяю цены...", reply_markup=get_main_keyboard())
    await check_prices_for_user(message.from_user.id)
    await message.answer("✅ Проверка завершена!", reply_markup=get_main_keyboard())

@dp.message(F.text == "💳 Пополнить Steam")
async def cmd_refill(message: types.Message):
    register_user(message.from_user.id)
    await message.answer(
        "💳 <b>Пополнение Steam со скидкой!</b>\n\n"
        "🎁 Быстрое пополнение баланса Steam\n"
        "✅ Работает для RU/UA/KZ/СНГ\n"
        "⚡ Мгновенное зачисление 24/7\n"
        "Нажми на кнопку ниже, чтобы пополнить баланс:",
        parse_mode="HTML",
        reply_markup=get_steam_refill_keyboard()
    )

@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: types.Message):
    register_user(message.from_user.id)
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM monitored_games WHERE user_id=?", (message.from_user.id,))
    count = c.fetchone()[0]
    conn.close()
    await message.answer(
        f"⚙️ <b>Настройки мониторинга</b>\n\n📊 Отслеживается игр: {count}\n🔔 Уведомления: Включены\n⏱ Интервал проверки: 1 час\n\n"
        f"<b>Условия уведомлений:</b>\n✅ Любое изменение цены\n✅ Появление скидки\n✅ Увеличение скидки\n\n<b>Команды:</b>\n/remove [ID] - удалить игру\n",
        parse_mode="HTML", reply_markup=get_main_keyboard()
    )

@dp.message(F.text)
async def handle_steam_link(message: types.Message):
    register_user(message.from_user.id)
    if 'steampowered.com' not in message.text and 'steamcommunity.com' not in message.text:
        await message.answer("❌ Пожалуйста, отправьте ссылку на игру из Steam", reply_markup=get_main_keyboard())
        return
    app_id = await extract_appid_from_url(message.text)
    if not app_id:
        await message.answer("❌ Не удалось извлечь ID игры из ссылки", reply_markup=get_main_keyboard())
        return
    processing_msg = await message.answer("⏳ Получаю информацию об игре...")
    game_info = await get_game_info(app_id)
    if not game_info:
        await processing_msg.edit_text("❌ Не удалось получить информацию об игре. Проверьте ссылку.")
        return
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    try:
        c.execute("""INSERT OR REPLACE INTO monitored_games 
                     (user_id, app_id, game_name, initial_price, current_price, discount, last_check, last_notified_price, last_notified_discount)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (message.from_user.id, app_id, game_info['name'], game_info['price'], game_info['final_price'], 
                   game_info['discount'], datetime.now().isoformat(), game_info['final_price'], game_info['discount']))
        conn.commit()
        response_text = f"✅ <b>Игра добавлена в мониторинг!</b>\n\n🎮 <b>{game_info['name']}</b>\n🆔 ID: <code>{app_id}</code>\n"
        if game_info['is_free']:
            response_text += f"💚 Бесплатная игра\n"
        else:
            response_text += f"💰 Цена: {game_info['final_price']:.2f} {game_info['currency']}\n"
            if game_info['discount'] > 0:
                response_text += f"🔥 Скидка: {game_info['discount']}%\n💵 Обычная цена: {game_info['price']:.2f} {game_info['currency']}\n"
        response_text += f"\n🔔 Вы получите уведомление при любом изменении цены или скидки!"
        await processing_msg.edit_text(response_text, parse_mode="HTML")
    except Exception as e:
        await processing_msg.edit_text(f"❌ Ошибка при сохранении: {e}")
    finally:
        conn.close()

async def check_prices_for_user(user_id: int):
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    c.execute("SELECT app_id, game_name, current_price, discount, last_notified_price, last_notified_discount FROM monitored_games WHERE user_id=?", (user_id,))
    games = c.fetchall()
    for app_id, game_name, old_price, old_discount, last_notified_price, last_notified_discount in games:
        await asyncio.sleep(2)
        game_info = await get_game_info(app_id)
        if game_info and not game_info['is_free']:
            new_price = game_info['final_price']
            new_discount = game_info['discount']
            c.execute("UPDATE monitored_games SET current_price=?, discount=?, last_check=? WHERE user_id=? AND app_id=?",
                     (new_price, new_discount, datetime.now().isoformat(), user_id, app_id))
            conn.commit()
            should_notify = False
            notification = ""
            if new_discount > 0 and old_discount == 0:
                should_notify = True
                notification = f"🔥 <b>ПОЯВИЛАСЬ СКИДКА!</b>\n\n🎮 {game_name}\n💰 Новая цена: {new_price:.2f} ₽\n📉 Скидка: {new_discount}%\n💵 Было: {game_info['price']:.2f} ₽\n💾 Экономия: {game_info['price'] - new_price:.2f} ₽\n\n🔗 https://store.steampowered.com/app/{app_id}/"
            elif new_discount > old_discount and new_discount > 0:
                should_notify = True
                notification = f"📈 <b>СКИДКА УВЕЛИЧИЛАСЬ!</b>\n\n🎮 {game_name}\n💰 Новая цена: {new_price:.2f} ₽\n📉 Скидка: {new_discount}% (было {old_discount}%)\n💵 Обычная цена: {game_info['price']:.2f} ₽\n💾 Экономия: {game_info['price'] - new_price:.2f} ₽\n\n🔗 https://store.steampowered.com/app/{app_id}/"
            elif new_price != last_notified_price and abs(new_price - last_notified_price) > 0.01:
                should_notify = True
                price_change = new_price - last_notified_price
                emoji = "📉" if price_change < 0 else "📈"
                change_text = "снизилась" if price_change < 0 else "повысилась"
                notification = f"{emoji} <b>ЦЕНА ИЗМЕНИЛАСЬ!</b>\n\n🎮 {game_name}\n💰 Новая цена: {new_price:.2f} ₽\n📊 Было: {last_notified_price:.2f} ₽\n🔄 Изменение: {abs(price_change):.2f} ₽ ({change_text})\n"
                if new_discount > 0:
                    notification += f"🔥 Скидка: {new_discount}%\n"
                notification += f"\n🔗 https://store.steampowered.com/app/{app_id}/"
            if should_notify:
                try:
                    await bot.send_message(user_id, notification, parse_mode="HTML")
                    c.execute("UPDATE monitored_games SET last_notified_price=?, last_notified_discount=? WHERE user_id=? AND app_id=?",
                             (new_price, new_discount, user_id, app_id))
                    conn.commit()
                except:
                    pass
    conn.close()

async def periodic_price_check():
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            conn = sqlite3.connect('steam_monitor.db')
            c = conn.cursor()
            c.execute("SELECT DISTINCT user_id FROM monitored_games")
            users = c.fetchall()
            conn.close()
            for (user_id,) in users:
                try:
                    await check_prices_for_user(user_id)
                    await asyncio.sleep(5)
                except:
                    pass
        except:
            await asyncio.sleep(60)

async def periodic_advertisement():
    """Рассылка рекламы каждые 10 минут"""
    while True:
        try:
            await asyncio.sleep(AD_INTERVAL)
            conn = sqlite3.connect('steam_monitor.db')
            c = conn.cursor()
            c.execute("SELECT user_id FROM users")
            users = c.fetchall()
            conn.close()
            
            ad_message = (
                "💎 <b>Специальное предложение!</b>\n\n"
                "💳 Пополни баланс Steam со скидкой!\n\n"
                "🎁 <b>Преимущества:</b>\n"
                "✅ Мгновенное зачисление\n"
                "✅ Работает 24/7\n"
                "✅ Подарок к каждому пополнению\n"
                "✅ Поддержка RU/UA/KZ/СНГ\n\n"
                "👇 Жми на кнопку ниже!"
            )
            
            for (user_id,) in users:
                try:
                    await bot.send_message(
                        user_id,
                        ad_message,
                        parse_mode="HTML",
                        reply_markup=get_steam_refill_keyboard()
                    )
                    await asyncio.sleep(1)  # Задержка между отправками, чтобы не получить бан
                except Exception as e:
                    # Игнорируем ошибки (например, если пользователь заблокировал бота)
                    pass
        except Exception as e:
            await asyncio.sleep(60)

async def main():
    init_db()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except:
        pass
    asyncio.create_task(periodic_price_check())
    asyncio.create_task(periodic_advertisement())
    print("🚀 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
