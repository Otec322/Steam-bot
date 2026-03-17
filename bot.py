import asyncio
import re
import sqlite3
from datetime import datetime
from typing import Optional, Dict, List, Any
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

BOT_TOKEN = "BOT_TOKEN"
CHECK_INTERVAL = 3600
AD_INTERVAL = 600

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class SearchStates(StatesGroup):
    browsing_results = State()

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Мои игры"), KeyboardButton(text="🔄 Проверить цены")],
            [KeyboardButton(text="💳 Пополнить Steam"), KeyboardButton(text="🔥 Топ‑10 скидок")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True
    )

def get_steam_refill_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💳 Пополнить Steam со скидкой!",
            url="https://ggsel.net/catalog/product/3-popolnenie-steam-ua-ru-kz-sng-24-7-podarok-5051848"
        )]
    ])

def get_search_results_keyboard(results: List[Dict], page: int, total_pages: int):
    buttons = []
    for game in results:
        name = game['name'][:38] + "..." if len(game['name']) > 38 else game['name']
        price_text = f" — {game['price']:.0f}₽" if game['price'] > 0 else " — Бесплатно"
        discount_text = f" 🔥-{game['discount']}%" if game['discount'] > 0 else ""
        buttons.append([InlineKeyboardButton(
            text=f"🎮 {name}{price_text}{discount_text}",
            callback_data=f"add_game:{game['appid']}"
        )])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{page + 1}"))
    if nav_buttons:
        buttons.append(nav_buttons)
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_search")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_games_list_keyboard(games: List, page: int, total_pages: int):
    buttons = []
    for app_id, name, price, discount in games:
        discount_text = f" 🔥-{discount}%" if discount > 0 else ""
        short_name = name[:33] + "..." if len(name) > 33 else name
        buttons.append([InlineKeyboardButton(
            text=f"🎮 {short_name}{discount_text}",
            callback_data=f"game_info:{app_id}"
        )])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"mypage:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"mypage:{page + 1}"))
    if nav_buttons:
        buttons.append(nav_buttons)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_game_detail_keyboard(app_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Открыть в Steam", url=f"https://store.steampowered.com/app/{app_id}/")],
        [InlineKeyboardButton(text="🗑 Удалить из мониторинга", callback_data=f"remove_game:{app_id}")],
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="back_to_list")]
    ])

def get_top_discounts_keyboard(games: List[Dict]):
    buttons = []
    for game in games[:10]:
        name = game['name'][:38] + "..." if len(game['name']) > 38 else game['name']
        buttons.append([InlineKeyboardButton(
            text=f"➕ {name} — {game['final_price']}₽ (-{game['discount']}%)",
            callback_data=f"add_game:{game['appid']}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_steam_header_image(app_id: int) -> str:
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/header.jpg"

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

async def get_game_reviews(app_id: int) -> Optional[Dict[str, Any]]:
    url = f"https://store.steampowered.com/appreviews/{app_id}?json=1&language=russian&num_per_page=0"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('success') == 1:
                        summary = data.get('query_summary', {})
                        total_reviews = summary.get('total_reviews', 0)
                        positive = summary.get('total_positive', 0)
                        if total_reviews > 0:
                            rating_percent = round(positive / total_reviews * 100, 1)
                            return {
                                'total': total_reviews,
                                'positive': positive,
                                'percent': rating_percent
                            }
    except:
        pass
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
                        info = {
                            'name': game_data.get('name', 'Unknown'),
                            'header_image': game_data.get('header_image', '') or get_steam_header_image(app_id),
                            'developers': game_data.get('developers', []),
                            'genres': [g['description'] for g in game_data.get('genres', [])],
                            'release_date': game_data.get('release_date', {}).get('date', 'Неизвестно'),
                            'is_free': game_data.get('is_free', False)
                        }
                        if 'price_overview' in game_data:
                            price_info = game_data['price_overview']
                            info.update({
                                'price': price_info['initial'] / 100,
                                'final_price': price_info['final'] / 100,
                                'discount': price_info['discount_percent'],
                                'currency': price_info['currency'],
                            })
                        else:
                            info.update({
                                'price': 0,
                                'final_price': 0,
                                'discount': 0,
                                'currency': 'RUB'
                            })
                        return info
    except:
        pass
    return None

async def get_game_details(app_id: int) -> Optional[Dict]:
    game_info = await get_game_info(app_id)
    if not game_info:
        return None
    reviews = await get_game_reviews(app_id)
    if reviews:
        game_info['rating'] = reviews['percent']
        game_info['reviews_total'] = reviews['total']
    else:
        game_info['rating'] = None
        game_info['reviews_total'] = 0
    return game_info

async def search_games_by_name(query: str) -> List[Dict]:
    url = f"https://store.steampowered.com/api/storesearch/?term={query}&l=russian&cc=ru"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and 'items' in data:
                        results = []
                        for item in data['items']:
                            price_info = item.get('price', {})
                            results.append({
                                'appid': item['id'],
                                'name': item['name'],
                                'price': price_info.get('final', 0) / 100 if price_info else 0,
                                'discount': price_info.get('discount_percent', 0) if price_info else 0,
                                'currency': 'RUB'
                            })
                        return results
    except:
        pass
    return []

async def get_top_discounts(limit: int = 10) -> List[Dict]:
    url = "https://store.steampowered.com/api/featuredcategories?cc=ru&l=russian"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    specials = data.get('specials', {}).get('items', [])
                    if not specials:
                        for cat in data.values():
                            if isinstance(cat, dict) and cat.get('name') == 'Специальные предложения':
                                specials = cat.get('items', [])
                                break
                    games = []
                    for item in specials[:limit]:
                        game = {
                            'appid': item['id'],
                            'name': item['name'],
                            'final_price': item.get('final_price', 0) / 100,
                            'discount': item.get('discount_percent', 0),
                            'header_image': item.get('header_image', '') or get_steam_header_image(item['id'])
                        }
                        games.append(game)
                    return games
    except:
        pass
    return []

async def add_game_to_monitoring(user_id: int, app_id: int) -> Optional[Dict]:
    game_info = await get_game_info(app_id)
    if not game_info:
        return None
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    try:
        c.execute("""INSERT OR REPLACE INTO monitored_games
                     (user_id, app_id, game_name, initial_price, current_price, discount,
                      last_check, last_notified_price, last_notified_discount)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (user_id, app_id, game_info['name'], game_info['price'],
                   game_info['final_price'], game_info['discount'],
                   datetime.now().isoformat(), game_info['final_price'], game_info['discount']))
        conn.commit()
    finally:
        conn.close()
    text = f"✅ <b>Игра добавлена в мониторинг!</b>\n\n🎮 <b>{game_info['name']}</b>\n🆔 ID: <code>{app_id}</code>\n"
    if game_info['is_free']:
        text += "💚 Бесплатная игра\n"
    else:
        text += f"💰 Цена: {game_info['final_price']:.2f} {game_info['currency']}\n"
        if game_info['discount'] > 0:
            text += f"🔥 Скидка: {game_info['discount']}%\n💵 Обычная цена: {game_info['price']:.2f} {game_info['currency']}\n"
    text += "\n🔔 Уведомлю при любом изменении цены или скидки!"
    return {'text': text, 'image': game_info.get('header_image', '') or get_steam_header_image(app_id)}

def build_game_info_text(app_id: int, name: str, price: float, discount: int,
                         release_date: str, rating: Optional[float] = None) -> str:
    text = f"🎮 <b>{name}</b>\n🆔 AppID: <code>{app_id}</code>\n\n"
    if discount > 0:
        original = price / (1 - discount / 100) if discount < 100 else price
        text += f"💰 Цена: <b>{price:.2f} ₽</b>\n"
        text += f"🔥 Скидка: <b>{discount}%</b>\n"
        text += f"💵 Без скидки: {original:.2f} ₽\n"
        text += f"💾 Экономия: {original - price:.2f} ₽\n"
    elif price == 0:
        text += "💚 <b>Бесплатная игра</b>\n"
    else:
        text += f"💰 Цена: <b>{price:.2f} ₽</b>\n"
    text += f"📅 Дата выхода: {release_date}\n"
    if rating is not None:
        if rating >= 80:
            emoji = "🌟"
        elif rating >= 60:
            emoji = "👍"
        else:
            emoji = "👎"
        text += f"{emoji} Рейтинг: <b>{rating}%</b> положительных\n"
    else:
        text += "⭐ Рейтинг: нет данных\n"
    text += f"\n🔔 Мониторинг активен"
    return text

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    register_user(message.from_user.id)
    await message.answer(
        "🎮 <b>Steam Price Monitor Bot</b>\n\n"
        "Отслеживайте цены и скидки на игры в Steam!\n\n"
        "<b>Как добавить игру:</b>\n"
        "• Напишите название — покажу варианты\n"
        "• Отправьте ссылку из Steam\n"
        "• Отправьте числовой AppID\n\n"
        "Бот пришлёт уведомление при любом изменении цены 👇",
        parse_mode="HTML", reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "❓ Помощь")
@dp.message(Command("help"))
async def cmd_help(message: types.Message, state: FSMContext):
    await state.clear()
    register_user(message.from_user.id)
    await message.answer(
        "📖 <b>Как пользоваться:</b>\n\n"
        "1️⃣ Напишите название: <code>Counter-Strike</code>\n"
        "2️⃣ Или ссылку: <code>https://store.steampowered.com/app/730/</code>\n"
        "3️⃣ Или AppID: <code>730</code>\n\n"
        "<b>📊 Мои игры:</b>\n"
        "Нажмите на игру → карточка с картинкой, ценой, рейтингом и датой выхода\n"
        "Там же кнопка удаления\n\n"
        "<b>🔥 Топ‑10 скидок:</b>\n"
        "Показывает актуальные предложения Steam\n\n"
        "<b>Команды:</b>\n"
        "/add [ID] — добавить по AppID\n"
        "/remove [ID] — удалить по AppID\n"
        "/top — топ-10 скидок\n\n"
        "💡 <i>Проверка цен — каждый час</i>",
        parse_mode="HTML", reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "📊 Мои игры")
@dp.message(Command("list"))
async def cmd_list(message: types.Message, state: FSMContext):
    await state.clear()
    register_user(message.from_user.id)
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    c.execute("SELECT app_id, game_name, current_price, discount FROM monitored_games WHERE user_id=?",
              (message.from_user.id,))
    games = c.fetchall()
    conn.close()
    if not games:
        await message.answer(
            "📭 У вас нет отслеживаемых игр.\n\nПросто напишите название игры в чат!",
            parse_mode="HTML", reply_markup=get_main_keyboard()
        )
        return
    page = 0
    page_size = 7
    total_pages = (len(games) + page_size - 1) // page_size
    await message.answer(
        f"📊 <b>Ваши отслеживаемые игры</b> ({len(games)} шт.)\n\n"
        f"<i>Нажмите на игру, чтобы посмотреть детали:</i>",
        parse_mode="HTML",
        reply_markup=get_games_list_keyboard(games[:page_size], page, total_pages)
    )

@dp.callback_query(F.data.startswith("mypage:"))
async def handle_mypage_change(callback: types.CallbackQuery):
    page = int(callback.data.split(":")[1])
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    c.execute("SELECT app_id, game_name, current_price, discount FROM monitored_games WHERE user_id=?",
              (callback.from_user.id,))
    games = c.fetchall()
    conn.close()
    page_size = 7
    total_pages = (len(games) + page_size - 1) // page_size
    page_games = games[page * page_size:(page + 1) * page_size]
    await callback.message.edit_reply_markup(
        reply_markup=get_games_list_keyboard(page_games, page, total_pages)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("game_info:"))
async def handle_game_info(callback: types.CallbackQuery):
    app_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    c.execute("SELECT game_name, current_price, discount FROM monitored_games WHERE user_id=? AND app_id=?",
              (callback.from_user.id, app_id))
    row = c.fetchone()
    conn.close()
    if not row:
        await callback.answer("❌ Игра не найдена.")
        return
    name, price, discount = row
    details = await get_game_details(app_id)
    if not details:
        await callback.answer("❌ Не удалось получить детали игры.")
        return
    release_date = details.get('release_date', 'Неизвестно')
    rating = details.get('rating')
    text = build_game_info_text(app_id, name, price, discount, release_date, rating)
    image_url = get_steam_header_image(app_id)
    await callback.message.delete()
    await bot.send_photo(
        callback.from_user.id,
        photo=image_url,
        caption=text,
        parse_mode="HTML",
        reply_markup=get_game_detail_keyboard(app_id)
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_list")
async def handle_back_to_list(callback: types.CallbackQuery):
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    c.execute("SELECT app_id, game_name, current_price, discount FROM monitored_games WHERE user_id=?",
              (callback.from_user.id,))
    games = c.fetchall()
    conn.close()
    await callback.message.delete()
    if not games:
        await bot.send_message(callback.from_user.id, "📭 У вас больше нет отслеживаемых игр.",
                               reply_markup=get_main_keyboard())
        await callback.answer()
        return
    page_size = 7
    total_pages = (len(games) + page_size - 1) // page_size
    await bot.send_message(
        callback.from_user.id,
        f"📊 <b>Ваши отслеживаемые игры</b> ({len(games)} шт.)\n\n"
        f"<i>Нажмите на игру, чтобы посмотреть детали:</i>",
        parse_mode="HTML",
        reply_markup=get_games_list_keyboard(games[:page_size], 0, total_pages)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("remove_game:"))
async def handle_remove_game(callback: types.CallbackQuery):
    app_id = int(callback.data.split(":")[1])
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    c.execute("SELECT game_name FROM monitored_games WHERE user_id=? AND app_id=?",
              (callback.from_user.id, app_id))
    row = c.fetchone()
    if not row:
        conn.close()
        await callback.answer("❌ Игра не найдена.")
        return
    game_name = row[0]
    c.execute("DELETE FROM monitored_games WHERE user_id=? AND app_id=?",
              (callback.from_user.id, app_id))
    conn.commit()
    c.execute("SELECT app_id, game_name, current_price, discount FROM monitored_games WHERE user_id=?",
              (callback.from_user.id,))
    games = c.fetchall()
    conn.close()
    await callback.answer(f"✅ «{game_name}» удалена!")
    await callback.message.delete()
    if games:
        page_size = 7
        total_pages = (len(games) + page_size - 1) // page_size
        await bot.send_message(
            callback.from_user.id,
            f"📊 <b>Ваши отслеживаемые игры</b> ({len(games)} шт.)\n\n"
            f"<i>Нажмите на игру, чтобы посмотреть детали:</i>",
            parse_mode="HTML",
            reply_markup=get_games_list_keyboard(games[:page_size], 0, total_pages)
        )
    else:
        await bot.send_message(
            callback.from_user.id,
            "📭 У вас больше нет отслеживаемых игр.",
            reply_markup=get_main_keyboard()
        )

@dp.callback_query(F.data.startswith("page:"))
async def handle_page_change(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[1])
    data = await state.get_data()
    results = data.get('results', [])
    page_size = 5
    total_pages = (len(results) + page_size - 1) // page_size
    page_results = results[page * page_size:(page + 1) * page_size]
    await state.update_data(page=page)
    await callback.message.edit_reply_markup(
        reply_markup=get_search_results_keyboard(page_results, page, total_pages)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("add_game:"))
async def handle_add_game(callback: types.CallbackQuery, state: FSMContext):
    app_id = int(callback.data.split(":")[1])
    await callback.answer("⏳ Добавляю...")
    result = await add_game_to_monitoring(callback.from_user.id, app_id)
    await state.clear()
    await callback.message.delete()
    if result:
        await bot.send_photo(
            callback.from_user.id,
            photo=result['image'],
            caption=result['text'],
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    else:
        await bot.send_message(
            callback.from_user.id,
            "❌ Не удалось получить информацию об игре.",
            reply_markup=get_main_keyboard()
        )

@dp.callback_query(F.data == "cancel_search")
async def handle_cancel_search(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Поиск отменён.")
    await bot.send_message(callback.from_user.id, "Выберите действие:", reply_markup=get_main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "noop")
async def handle_noop(callback: types.CallbackQuery):
    await callback.answer()

@dp.message(Command("add"))
async def cmd_add(message: types.Message):
    register_user(message.from_user.id)
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("❌ Использование: /add [AppID]\nПример: /add 730", reply_markup=get_main_keyboard())
        return
    try:
        app_id = int(parts[1])
    except ValueError:
        await message.answer("❌ AppID должен быть числом.", reply_markup=get_main_keyboard())
        return
    processing_msg = await message.answer("⏳ Получаю информацию об игре...")
    result = await add_game_to_monitoring(message.from_user.id, app_id)
    await processing_msg.delete()
    if result:
        await message.answer_photo(
            photo=result['image'],
            caption=result['text'],
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer("❌ Игра с таким AppID не найдена.", reply_markup=get_main_keyboard())

@dp.message(Command("remove"))
async def cmd_remove(message: types.Message):
    register_user(message.from_user.id)
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "❌ Использование: /remove [AppID]\nПример: /remove 730\n\nИли откройте 📊 Мои игры и удалите там.",
            reply_markup=get_main_keyboard()
        )
        return
    try:
        app_id = int(parts[1])
    except ValueError:
        await message.answer("❌ AppID должен быть числом.", reply_markup=get_main_keyboard())
        return
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    c.execute("SELECT game_name FROM monitored_games WHERE user_id=? AND app_id=?",
              (message.from_user.id, app_id))
    row = c.fetchone()
    if row:
        c.execute("DELETE FROM monitored_games WHERE user_id=? AND app_id=?",
                  (message.from_user.id, app_id))
        conn.commit()
        await message.answer(f"✅ <b>«{row[0]}»</b> удалена из мониторинга!",
                             parse_mode="HTML", reply_markup=get_main_keyboard())
    else:
        await message.answer("❌ Игра не найдена в вашем списке.", reply_markup=get_main_keyboard())
    conn.close()

@dp.message(F.text == "🔄 Проверить цены")
@dp.message(Command("check"))
async def cmd_check(message: types.Message, state: FSMContext):
    await state.clear()
    register_user(message.from_user.id)
    await message.answer("🔄 Проверяю цены...", reply_markup=get_main_keyboard())
    await check_prices_for_user(message.from_user.id)
    await message.answer("✅ Проверка завершена!", reply_markup=get_main_keyboard())

@dp.message(F.text == "💳 Пополнить Steam")
async def cmd_refill(message: types.Message):
    register_user(message.from_user.id)
    await message.answer(
        "💳 <b>Пополнение Steam со скидкой!</b>\n\n"
        "✅ Мгновенное зачисление\n"
        "✅ Работает 24/7\n"
        "✅ Подарок к каждому пополнению\n"
        "✅ Поддержка RU/UA/KZ/СНГ\n\n"
        "👇 Жми на кнопку ниже:",
        parse_mode="HTML",
        reply_markup=get_steam_refill_keyboard()
    )

@dp.message(F.text == "🔥 Топ‑10 скидок")
@dp.message(Command("top"))
async def cmd_top_discounts(message: types.Message):
    register_user(message.from_user.id)
    waiting = await message.answer("🔍 Ищу лучшие предложения Steam...")
    games = await get_top_discounts(10)
    if not games:
        await waiting.edit_text("❌ Не удалось получить список скидок. Попробуйте позже.")
        return
    text = "🔥 <b>Топ‑10 игр со скидками прямо сейчас</b>\n\n"
    for i, game in enumerate(games, 1):
        text += f"{i}. <b>{game['name']}</b>\n"
        text += f"   💰 {game['final_price']}₽  🔥 -{game['discount']}%\n"
        text += f"   🆔 <code>{game['appid']}</code>\n\n"
    await waiting.delete()
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=get_top_discounts_keyboard(games)
    )

@dp.message(F.text)
async def handle_text_input(message: types.Message, state: FSMContext):
    register_user(message.from_user.id)
    text = message.text.strip()
    if 'steampowered.com' in text or 'steamcommunity.com' in text:
        app_id = await extract_appid_from_url(text)
        if not app_id:
            await message.answer("❌ Не удалось извлечь ID из ссылки.", reply_markup=get_main_keyboard())
            return
        processing_msg = await message.answer("⏳ Получаю информацию об игре...")
        result = await add_game_to_monitoring(message.from_user.id, app_id)
        await processing_msg.delete()
        if result:
            await message.answer_photo(
                photo=result['image'],
                caption=result['text'],
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
        else:
            await message.answer("❌ Не удалось получить информацию об игре.", reply_markup=get_main_keyboard())
        return
    if text.isdigit():
        processing_msg = await message.answer("⏳ Получаю информацию об игре...")
        result = await add_game_to_monitoring(message.from_user.id, int(text))
        await processing_msg.delete()
        if result:
            await message.answer_photo(
                photo=result['image'],
                caption=result['text'],
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
        else:
            await message.answer("❌ Игра с таким AppID не найдена.", reply_markup=get_main_keyboard())
        return
    searching_msg = await message.answer(f"🔍 Ищу «{text}»...")
    results = await search_games_by_name(text)
    if not results:
        await searching_msg.edit_text("❌ Ничего не найдено. Попробуйте другое название или ссылку из Steam.")
        await message.answer("Выберите действие:", reply_markup=get_main_keyboard())
        return
    page_size = 5
    total_pages = (len(results) + page_size - 1) // page_size
    await state.set_state(SearchStates.browsing_results)
    await state.update_data(results=results, page=0)
    await searching_msg.edit_text(
        f"🔍 <b>Результаты: «{text}»</b>\n"
        f"<i>Найдено: {len(results)} игр. Выберите нужную:</i>",
        parse_mode="HTML",
        reply_markup=get_search_results_keyboard(results[:page_size], 0, total_pages)
    )

async def check_prices_for_user(user_id: int):
    conn = sqlite3.connect('steam_monitor.db')
    c = conn.cursor()
    c.execute(
        "SELECT app_id, game_name, current_price, discount, last_notified_price, last_notified_discount "
        "FROM monitored_games WHERE user_id=?",
        (user_id,))
    games = c.fetchall()
    for app_id, game_name, old_price, old_discount, last_notified_price, last_notified_discount in games:
        await asyncio.sleep(2)
        game_info = await get_game_info(app_id)
        if game_info and not game_info['is_free']:
            new_price = game_info['final_price']
            new_discount = game_info['discount']
            c.execute(
                "UPDATE monitored_games SET current_price=?, discount=?, last_check=? WHERE user_id=? AND app_id=?",
                (new_price, new_discount, datetime.now().isoformat(), user_id, app_id))
            conn.commit()
            should_notify = False
            notification = ""
            if new_discount > 0 and old_discount == 0:
                should_notify = True
                notification = (
                    f"🔥 <b>ПОЯВИЛАСЬ СКИДКА!</b>\n\n🎮 {game_name}\n"
                    f"💰 Новая цена: {new_price:.2f} ₽\n📉 Скидка: {new_discount}%\n"
                    f"💵 Было: {game_info['price']:.2f} ₽\n"
                    f"💾 Экономия: {game_info['price'] - new_price:.2f} ₽\n\n"
                    f"🔗 https://store.steampowered.com/app/{app_id}/"
                )
            elif new_discount > old_discount and new_discount > 0:
                should_notify = True
                notification = (
                    f"📈 <b>СКИДКА УВЕЛИЧИЛАСЬ!</b>\n\n🎮 {game_name}\n"
                    f"💰 Новая цена: {new_price:.2f} ₽\n"
                    f"📉 Скидка: {new_discount}% (было {old_discount}%)\n"
                    f"💵 Обычная цена: {game_info['price']:.2f} ₽\n"
                    f"💾 Экономия: {game_info['price'] - new_price:.2f} ₽\n\n"
                    f"🔗 https://store.steampowered.com/app/{app_id}/"
                )
            elif abs(new_price - last_notified_price) > 0.01:
                should_notify = True
                price_change = new_price - last_notified_price
                emoji = "📉" if price_change < 0 else "📈"
                change_text = "снизилась" if price_change < 0 else "повысилась"
                notification = (
                    f"{emoji} <b>ЦЕНА ИЗМЕНИЛАСЬ!</b>\n\n🎮 {game_name}\n"
                    f"💰 Новая цена: {new_price:.2f} ₽\n"
                    f"📊 Было: {last_notified_price:.2f} ₽\n"
                    f"🔄 Изменение: {abs(price_change):.2f} ₽ ({change_text})\n"
                )
                if new_discount > 0:
                    notification += f"🔥 Скидка: {new_discount}%\n"
                notification += f"\n🔗 https://store.steampowered.com/app/{app_id}/"
            if should_notify:
                try:
                    image_url = get_steam_header_image(app_id)
                    await bot.send_photo(
                        user_id,
                        photo=image_url,
                        caption=notification,
                        parse_mode="HTML"
                    )
                    c.execute(
                        "UPDATE monitored_games SET last_notified_price=?, last_notified_discount=? "
                        "WHERE user_id=? AND app_id=?",
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
                "✅ Мгновенное зачисление\n"
                "✅ Работает 24/7\n"
                "✅ Подарок к каждому пополнению\n"
                "✅ Поддержка RU/UA/KZ/СНГ\n\n"
                "👇 Жми на кнопку ниже!"
            )
            for (user_id,) in users:
                try:
                    await bot.send_message(
                        user_id, ad_message,
                        parse_mode="HTML",
                        reply_markup=get_steam_refill_keyboard()
                    )
                    await asyncio.sleep(1)
                except:
                    pass
        except:
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
