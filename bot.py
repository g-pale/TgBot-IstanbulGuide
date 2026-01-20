import os
import sys
import logging
import tempfile
import yaml
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from openai import OpenAI
from dotenv import load_dotenv
import aiohttp
import re
from collections import defaultdict, deque
from datetime import datetime

# Глобальная переменная для базы
ISTANBUL_DATA = {}

# Константы для состояний диалога
STATE_AWAITING_SIGHTS_DISTRICT = "awaiting_sights_district"
STATE_AWAITING_FOOD_DISTRICT = "awaiting_food_district"
STATE_AWAITING_WEATHER_CITY = "awaiting_weather_city"

load_dotenv()  # Загружает переменные из .env в окружение

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация морфологического анализатора для русского языка
try:
    import pymorphy2
    morph = pymorphy2.MorphAnalyzer()
    MORPH_AVAILABLE = True
except ImportError:
    morph = None
    MORPH_AVAILABLE = False
    logger.warning("pymorphy2 не установлен, нормализация городов будет ограниченной")

# Bot token and OpenRouter API key
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
if not TOKEN:
    print("[ОШИБКА] Переменная окружения TELEGRAM_BOT_TOKEN не задана! Бот завершает работу.")
    sys.exit(1)
if not OPENROUTER_API_KEY:
    print("[ОШИБКА] Переменная окружения OPENROUTER_API_KEY не задана! Бот завершает работу.")
    sys.exit(1)

os.environ["OPENAI_API_KEY"] = OPENROUTER_API_KEY

# Глобальная инициализация OpenAI клиента
client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
    default_headers={
        "HTTP-Referer": "your-site.com",  # Замените на ваш сайт
        "X-Title": "Telegram Bot"         # Любое название
    }
)

# Шаблон для режима "Гид по Стамбулу"
PROMPT_TEMPLATE_ISTANBUL = """
Ты — надёжный Telegram-бот-гид по Стамбулу. Ты помогаешь туристам получить достоверную и полезную информацию по городу. Никогда не выдумывай факты — только реальные места, рестораны, районы и маршруты.

Отвечай чётко, структурировано и в одном сообщении. Не повторяй одни и те же пункты. Указывай конкретные локации и маршруты. Не добавляй общее вступление вроде "Конечно, вот маршрут...".

Формат:

1. Утро  
2. Обед  
3. День  
4. Вечер  

💡 Совет в конце.

Не повторяй ответ дважды. Ответ должен быть один, краткий и полезный.
""".strip()

# Шаблон для составления маршрута
PROMPT_TEMPLATE_ROUTE = """
Ты — опытный Telegram-гид по Стамбулу. Пользователь просит составить маршрут на 1 день.

Составь **логичный и реалистичный маршрут по городу**, учитывая географию, расстояния и время суток.

**Формат ответа:**

1. Утро (Султанахмет)  
– Айя-София  
– Голубая мечеть  
– Прогулка по Гипподрому  

2. Обед  
– Конкретное кафе/локанта рядом (вкусно и недорого)  

3. Послеобеденное время (Эминоню → Галата)  
– Гранд-базар или Египетский рынок  
– Прогулка до Галатской башни  
– Кофе с видом

4. Вечер (Таксим или Босфор)  
– Ужин с видом  
– Паром или набережная  
– Альтернативный вечер: улица Истикляль

💡 Совет: Добавь практичный совет (транспорт, Istanbulkart, лайфхак по очередям).

**Важно:** Не просто перечисляй места. Привяжи их ко времени суток и маршруту. Упоминай трамвай T1, паромы, районы. Ответ — один, без повторов.
""".strip()

# Шаблон для обычных ответов
DEFAULT_PROMPT_TEMPLATE = """
Ты — умный и краткий Telegram-бот. Отвечай на вопросы пользователей чётко, структурировано и по делу.

Если вопрос относится к прошлому диалогу — обязательно учитывай контекст.

Структурируй ответ в разделы **только если это уместно**, но не жертвуй фактами ради формы.

💡 В конце можешь добавить совет или интересный факт.
""".strip()

# OpenWeather API key
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
if not OPENWEATHER_API_KEY:
    logger.warning("OPENWEATHER_API_KEY не задан в переменных окружения. Функция погоды будет недоступна.")

# Глобальный словарь для истории сообщений (user_id -> deque)
user_histories = defaultdict(lambda: deque(maxlen=10))  # храним последние 10 сообщений

def chunk_text(text: str, max_len: int = 1000):
    """
    Разбивает текст на части длиной до max_len, стараясь разрезать по предложениям.
    """
    chunks = []
    current = ""
    for sentence in text.replace("\n", " ").split('. '):
        part = sentence + ('. ' if not sentence.endswith('.') else '')
        if len(current) + len(part) > max_len:
            if current:
                chunks.append(current.strip())
            current = part
        else:
            current += part
    if current:
        chunks.append(current.strip())
    return chunks

def clean_markdown_formatting(text: str) -> str:
    """
    Очищает текст от проблемных Markdown-символов, которые Telegram может не обработать.
    """
    # Убираем ### заголовки и заменяем на жирный текст
    text = re.sub(r'^#{1,6}\s*(.+)$', r'**\1**', text, flags=re.MULTILINE)
    
    # Убираем лишние переносы строк
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    return text.strip()

def split_into_telegram_chunks(text: str, limit: int = 3500) -> list:
    """
    Безопасно делит длинный текст на части для Telegram (лимит ~4096 символов).
    Деление выполняется по строкам, чтобы не ломать разметку Markdown/HTML.
    """
    parts = []
    current_lines = []
    current_len = 0

    for line in text.split('\n'):
        # +1 за перенос строки
        additional = len(line) + 1
        if current_len + additional > limit and current_lines:
            parts.append('\n'.join(current_lines).strip())
            current_lines = [line]
            current_len = len(line) + 1
        else:
            current_lines.append(line)
            current_len += additional

    if current_lines:
        parts.append('\n'.join(current_lines).strip())

    return parts

async def get_weather(city: str) -> str | None:
    """
    Получает текущую погоду для города (async версия с aiohttp).
    Упрощённая версия - прямой запрос по названию города.
    """
    if not OPENWEATHER_API_KEY:
        logger.warning("OPENWEATHER_API_KEY не задан")
        return None
    
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "q": city,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "ru"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    data = await response.json()
                    temp = data["main"]["temp"]
                    desc = data["weather"][0]["description"]
                    return f"Сейчас в {city} {temp}°C, {desc}."
                else:
                    logger.warning(f"Weather API error: status={response.status}, city={city}")
                    return None
    except aiohttp.ClientError as e:
        logger.error(f"Ошибка при запросе погоды для {city}: {e}")
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка при получении погоды для {city}: {e}")
        return None

async def get_weather_forecast(city: str, days: int = 3) -> str | None:
    """
    Получает прогноз погоды на несколько дней (async версия).
    """
    if not OPENWEATHER_API_KEY:
        logger.warning("OPENWEATHER_API_KEY не задан")
        return None
    
    url = "https://api.openweathermap.org/data/2.5/forecast"
    params = {
        "q": city,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "ru"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status != 200:
                    logger.warning(f"Forecast API error: status={response.status}, city={city}")
                    return None
                
                data = await response.json()
                if "list" not in data:
                    return None
                
                # Группируем по дням
                days_data = defaultdict(list)
                for entry in data["list"]:
                    date = datetime.fromtimestamp(entry["dt"]).strftime("%Y-%m-%d")
                    days_data[date].append(entry)
                
                result = []
                for i, (date, entries) in enumerate(sorted(days_data.items())):
                    if i >= days:
                        break
                    temps = [e["main"]["temp"] for e in entries]
                    desc = entries[0]["weather"][0]["description"]
                    avg_temp = sum(temps) / len(temps)
                    result.append(f"{date}: {avg_temp:.1f}°C, {desc}")
                
                return "\n".join(result) if result else None
                
    except aiohttp.ClientError as e:
        logger.error(f"Ошибка при запросе прогноза для {city}: {e}")
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка при получении прогноза для {city}: {e}")
        return None

def normalize_city(city):
    """
    Нормализует название города: приводит к именительному падежу используя pymorphy2.
    Универсальное решение без перечисления всех городов.
    """
    city = city.strip()
    if not city:
        return city
    
    # Небольшой словарь только для специальных случаев (многословные, иностранные)
    special_cases = {
        "нижнем": "Нижний Новгород",
        "нижнем новгороде": "Нижний Новгород",
        "нижний новгород": "Нижний Новгород",
        "санкт-петербурге": "Санкт-Петербург",
        "санкт-петербург": "Санкт-Петербург",
        "питере": "Санкт-Петербург",
        "питер": "Санкт-Петербург",
        "ростове-на-дону": "Ростов-на-Дону",
        "ростов-на-дону": "Ростов-на-Дону",
        # Иностранные города (только самые популярные)
        "нью-йорке": "New York",
        "мюнхене": "Munich",
        "лондоне": "London",
        "париже": "Paris",
        "берлине": "Berlin",
        "варшаве": "Warsaw",
        "барселоне": "Barcelona",
        "istanbul": "Istanbul",
        "стамбуле": "Istanbul",
        "стамбул": "Istanbul",
    }
    
    city_lower = city.lower()
    if city_lower in special_cases:
        return special_cases[city_lower]
    
    # Для русского языка используем pymorphy2 для приведения к именительному падежу
    if MORPH_AVAILABLE and any(ord(c) >= 0x0400 and ord(c) <= 0x04FF for c in city):
        # Это кириллица - используем морфологический анализ
        words = city.split()
        normalized_words = []
        
        for word in words:
            # Пропускаем дефисы и предлоги
            if word.lower() in ['на', 'в', 'по', 'для', '-']:
                continue
            # Приводим к нормальной форме (именительный падеж)
            parsed = morph.parse(word)[0]
            normal_form = parsed.normal_form
            # Сохраняем капитализацию первого символа
            if word[0].isupper():
                normal_form = normal_form.capitalize()
            normalized_words.append(normal_form)
        
        if normalized_words:
            return ' '.join(normalized_words)
    
    # Для неизвестных городов или если pymorphy2 недоступен - возвращаем с заглавной буквы
    return city.title()

def extract_city(raw_city):
    """
    Извлекает название города из текста запроса.
    Поддерживает многословные названия городов.
    """
    # Паттерн для поиска "в <город>" или "по <город>" или "для <город>"
    # Захватываем всё до конца строки или до следующего ключевого слова
    match = re.search(r'(?:в|по|для)\s+([а-яА-Яa-zA-ZёЁ\- ]+?)(?:\s|$|,|\.|\?|!)', raw_city, re.IGNORECASE)
    if match:
        city = match.group(1).strip()
        # Убираем лишние слова в конце (если есть)
        city = re.sub(r'\s+(сегодня|завтра|сейчас|погода|температура).*$', '', city, flags=re.IGNORECASE)
        if city:
            return city
    # Если паттерн не сработал, пробуем взять последние 1-3 слова
    words = raw_city.strip().split()
    if len(words) >= 2:
        # Берём последние 2-3 слова (для многословных городов)
        return ' '.join(words[-2:]) if len(words) >= 2 else words[-1]
    return raw_city.strip()

def format_markdown(text: str) -> str:
    """
    Унифицированная функция форматирования в Markdown.
    Убирает лишние пробелы, нормализует переносы строк, исправляет незакрытые теги.
    """
    if not text:
        return ""
    
    # Удаляем лишние пробелы и переносы строк
    text = re.sub(r'\n{3,}', '\n\n', text.strip())
    
    # Закрываем незакрытые **
    if text.count('**') % 2 != 0:
        text += '**'
    
    return text.strip()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    
    # Постоянное меню (ReplyKeyboardMarkup) - кнопки внизу экрана
    reply_keyboard = [
        ["🗺 Маршруты", "🏛 Достопримечательности"],
        ["🍽 Рестораны", "ℹ️ Помощь"],
        ["📄 Новый диалог", "🌍 Погода"]
    ]
    reply_markup = ReplyKeyboardMarkup(
        reply_keyboard, 
        resize_keyboard=True, 
        one_time_keyboard=False,
        input_field_placeholder="Выберите действие или задайте вопрос..."
    )
    
    # Отправляем одно сообщение с постоянным меню
    await update.message.reply_text(
        f"Привет, {user.first_name}! Я бот-гид по Стамбулу. Выберите, что вас интересует:",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "routes":
        keyboard = [
            [
                InlineKeyboardButton("1 день", callback_data="route_1"),
                InlineKeyboardButton("2 дня", callback_data="route_2"),
                InlineKeyboardButton("3 дня", callback_data="route_3")
            ],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "Выберите длительность маршрута:",
            reply_markup=reply_markup
        )

    elif query.data == "sights":
        reply_markup = create_district_keyboard("sights")
        await query.edit_message_text(
            "Выберите район для просмотра достопримечательностей:",
            reply_markup=reply_markup
        )

    elif query.data == "restaurants":
        reply_markup = create_district_keyboard("restaurants")
        await query.edit_message_text(
            "Выберите район для просмотра ресторанов:",
            reply_markup=reply_markup
        )

    elif query.data == "help":
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "Я понимаю команды:\n"
            "/start - Запустить бота\n"
            "/help - Показать справку\n"
            "/route - Показать маршрут на 1 день\n"
            "/sights <район> - Показать достопримечательности в районе\n"
            "/eat <район> - Показать рестораны в районе\n\n"
            "Также вы можете использовать кнопки меню для навигации.",
            reply_markup=reply_markup
        )

    elif query.data == "back_to_main":
        keyboard = [
            [
                InlineKeyboardButton("🗺 Маршруты", callback_data="routes"),
                InlineKeyboardButton("🏛 Достопримечательности", callback_data="sights")
            ],
            [
                InlineKeyboardButton("🍽 Рестораны", callback_data="restaurants"),
                InlineKeyboardButton("ℹ️ Помощь", callback_data="help")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "Выберите, что вас интересует:",
            reply_markup=reply_markup
        )

    elif query.data.startswith("route_"):
        days = query.data.split("_")[1]
        # Ищем маршрут по количеству дней
        route = None
        for r in ISTANBUL_DATA.get("routes", []):
            title = r.get("title", "").lower()
            if days == "1" and ("1 день" in title or "один день" in title or "классика" in title):
                route = r
                break
            elif days == "2" and ("2 дня" in title or "два дня" in title):
                route = r
                break
            elif days == "3" and ("3 дня" in title or "три дня" in title):
                # Предпочитаем обычный 3-дневный маршрут, если есть "лайтовый"
                if "лайтовый" not in title:
                    route = r
                elif not route:  # Если ещё не нашли, сохраняем лайтовый как запасной
                    route = r
        
        if not route:
            await query.edit_message_text("Маршрут не найден в базе данных.")
            return

        # Форматируем маршрут в Markdown
        lines = [f"**{route['title']}**"]
        for block in route.get("steps", []):
            time_block = block.get("time", "")
            lines.append(f"\n**{time_block}**")
            for act in block.get("activities", []):
                lines.append(f"• {act}")
        text = format_markdown("\n".join(lines))

        keyboard = [[InlineKeyboardButton("◀️ Назад к маршрутам", callback_data="routes")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    elif query.data.startswith("sights_"):
        district = query.data.split("_", 1)[1]  # Используем split с maxsplit=1 для многословных названий
        results = [
            sight for sight in ISTANBUL_DATA.get("sights", [])
            if normalize_district(sight.get("district", "")).lower() == normalize_district(district).lower()
        ]

        if not results:
            await query.edit_message_text(f"В районе {district} ничего не найдено в базе данных.")
            return

        # Форматируем в Markdown
        lines = [f"**🏛 Достопримечательности в районе {district}:**\n"]
        for s in results:
            name = s.get("name", "Без названия")
            desc = s.get("description", "")
            hours = s.get("opening_hours", "Уточняйте на месте")
            price = s.get("price", "Уточняйте на месте")
            transport = s.get("transport", "")
            
            lines.append(f"• **{name}**")
            if desc:
                lines.append(f"  {desc}")
            lines.append(f"  🕒 {hours}")
            lines.append(f"  💰 {price}")
            if transport:
                lines.append(f"  🚇 {transport}")
            lines.append("")  # Пустая строка между местами

        text = format_markdown("\n".join(lines))

        keyboard = [[InlineKeyboardButton("◀️ Назад к районам", callback_data="sights")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    elif query.data.startswith("eat_"):
        district = query.data.split("_", 1)[1]  # Используем split с maxsplit=1 для многословных названий
        results = [
            restaurant for restaurant in ISTANBUL_DATA.get("restaurants", [])
            if normalize_district(restaurant.get("district", "")).lower() == normalize_district(district).lower()
        ]

        if not results:
            await query.edit_message_text(f"В районе {district} ничего не найдено в базе данных.")
            return

        # Форматируем в Markdown
        lines = [f"**🍽 Рестораны в районе {district}:**\n"]
        for r in results:
            name = r.get("name", "Без названия")
            cuisine = r.get("cuisine", "")
            price = r.get("price_level", "")
            desc = r.get("description", "")
            hours = r.get("opening_hours", "Уточняйте на месте")
            address = r.get("address", "")
            transport = r.get("transport", "")
            tags = r.get("tags", [])
            
            lines.append(f"• **{name}**")
            if cuisine:
                lines.append(f"  🍳 {cuisine}")
            if price:
                lines.append(f"  💰 {price}")
            if desc:
                lines.append(f"  {desc}")
            lines.append(f"  🕒 {hours}")
            if address:
                lines.append(f"  📍 {address}")
            if transport:
                lines.append(f"  🚇 {transport}")
            if tags:
                lines.append(f"  {' '.join(['#' + tag.replace(' ', '_') for tag in tags])}")
            lines.append("")  # Пустая строка между ресторанами

        text = format_markdown("\n".join(lines))

        keyboard = [[InlineKeyboardButton("◀️ Назад к районам", callback_data="restaurants")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

def get_persistent_keyboard():
    """Возвращает постоянную клавиатуру для всех сообщений"""
    reply_keyboard = [
        ["🗺 Маршруты", "🏛 Достопримечательности"],
        ["🍽 Рестораны", "ℹ️ Помощь"],
        ["📄 Новый диалог", "🌍 Погода"]
    ]
    return ReplyKeyboardMarkup(
        reply_keyboard, 
        resize_keyboard=True, 
        one_time_keyboard=False,
        input_field_placeholder="Выберите действие или задайте вопрос..."
    )

def get_districts_from_data(data_type: str) -> list:
    """Получает список уникальных районов из базы данных."""
    districts = set()
    if data_type == "sights":
        for sight in ISTANBUL_DATA.get("sights", []):
            if sight.get("district"):
                districts.add(sight["district"])
    elif data_type == "restaurants":
        for restaurant in ISTANBUL_DATA.get("restaurants", []):
            if restaurant.get("district"):
                districts.add(restaurant["district"])
    return sorted(list(districts))

def create_district_keyboard(data_type: str) -> InlineKeyboardMarkup:
    """Создаёт inline-клавиатуру с районами для выбора."""
    districts = get_districts_from_data(data_type)
    keyboard = []
    
    # Размещаем кнопки по 2 в ряд
    for i in range(0, len(districts), 2):
        row = []
        row.append(InlineKeyboardButton(districts[i], callback_data=f"{data_type}_{districts[i]}"))
        if i + 1 < len(districts):
            row.append(InlineKeyboardButton(districts[i + 1], callback_data=f"{data_type}_{districts[i + 1]}"))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")])
    return InlineKeyboardMarkup(keyboard)

def create_context_menu(is_istanbul_related: bool):
    """Создаёт контекстное меню после ответа"""
    if is_istanbul_related:
        keyboard = [
            [
                InlineKeyboardButton("👍 Понятно", callback_data="ok"),
                InlineKeyboardButton("❓ Уточнить", callback_data="clarify")
            ],
            [
                InlineKeyboardButton("🗺 Маршрут", callback_data="route_1"),
                InlineKeyboardButton("🏛 Места", callback_data="sights")
            ],
            [InlineKeyboardButton("🔄 Новый вопрос", callback_data="new_question")]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("👍 Понятно", callback_data="ok"),
                InlineKeyboardButton("❓ Уточнить", callback_data="clarify")
            ],
            [InlineKeyboardButton("🔄 Новый вопрос", callback_data="new_question")]
        ]
    return InlineKeyboardMarkup(keyboard)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Я понимаю команды:\n"
        "/start - Запустить бота\n"
        "/help - Показать справку\n"
        "Просто отправьте мне сообщение, и я отвечу текстом и голосом.",
        reply_markup=get_persistent_keyboard()
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    user_data = context.user_data

    # Проверяем состояния диалога
    if user_data.get(STATE_AWAITING_SIGHTS_DISTRICT):
        # Пользователь выбрал район для достопримечательностей
        district = normalize_district(text)
        user_data.pop(STATE_AWAITING_SIGHTS_DISTRICT, None)
        
        results = [
            sight for sight in ISTANBUL_DATA.get("sights", [])
            if normalize_district(sight.get("district", "")).lower() == district.lower()
        ]
        
        if not results:
            await update.message.reply_text(
                f"В районе {district} ничего не найдено в базе данных.",
                reply_markup=get_persistent_keyboard()
            )
            return
        
        # Форматируем в Markdown
        lines = [f"**🏛 Достопримечательности в районе {district}:**\n"]
        for s in results:
            name = s.get("name", "Без названия")
            desc = s.get("description", "")
            hours = s.get("opening_hours", "Уточняйте на месте")
            price = s.get("price", "Уточняйте на месте")
            transport = s.get("transport", "")
            
            lines.append(f"• **{name}**")
            if desc:
                lines.append(f"  {desc}")
            lines.append(f"  🕒 {hours}")
            lines.append(f"  💰 {price}")
            if transport:
                lines.append(f"  🚇 {transport}")
            lines.append("")
        
        response_text = format_markdown("\n".join(lines))
        await update.message.reply_text(
            response_text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=get_persistent_keyboard()
        )
        return
    
    elif user_data.get(STATE_AWAITING_FOOD_DISTRICT):
        # Пользователь выбрал район для ресторанов
        district = normalize_district(text)
        user_data.pop(STATE_AWAITING_FOOD_DISTRICT, None)
        
        results = [
            restaurant for restaurant in ISTANBUL_DATA.get("restaurants", [])
            if normalize_district(restaurant.get("district", "")).lower() == district.lower()
        ]
        
        if not results:
            await update.message.reply_text(
                f"В районе {district} ничего не найдено в базе данных.",
                reply_markup=get_persistent_keyboard()
            )
            return
        
        # Форматируем в Markdown
        lines = [f"**🍽 Рестораны в районе {district}:**\n"]
        for r in results:
            name = r.get("name", "Без названия")
            cuisine = r.get("cuisine", "")
            price = r.get("price_level", "")
            desc = r.get("description", "")
            hours = r.get("opening_hours", "Уточняйте на месте")
            address = r.get("address", "")
            transport = r.get("transport", "")
            tags = r.get("tags", [])
            
            lines.append(f"• **{name}**")
            if cuisine:
                lines.append(f"  🍳 {cuisine}")
            if price:
                lines.append(f"  💰 {price}")
            if desc:
                lines.append(f"  {desc}")
            lines.append(f"  🕒 {hours}")
            if address:
                lines.append(f"  📍 {address}")
            if transport:
                lines.append(f"  🚇 {transport}")
            if tags:
                lines.append(f"  {' '.join(['#' + tag.replace(' ', '_') for tag in tags])}")
            lines.append("")
        
        response_text = format_markdown("\n".join(lines))
        await update.message.reply_text(
            response_text,
            parse_mode="Markdown",
            disable_web_page_preview=True,
            reply_markup=get_persistent_keyboard()
        )
        return

    if text == "ℹ️ Помощь":
        await help_command(update, context)
        return
    elif text == "📝 О боте":
        await update.message.reply_text(
            "Я Telegram-бот, использующий OpenRouter для генерации ответов с помощью ИИ."
        )
        return
    elif text == "📄 Новый диалог":
        user_histories[user_id].clear()
        user_data.clear()  # Сбрасываем состояния
        await update.message.reply_text(
            "Новый диалог начат! Можете задать свой вопрос.",
            reply_markup=get_persistent_keyboard()
        )
        return
    elif text == "🌍 Погода":
        await update.message.reply_text(
            "🌍 Укажите название города для получения информации о погоде.\n\n"
            "Например:\n"
            "• Погода в Москве\n"
            "• Температура в Санкт-Петербурге\n"
            "• Прогноз погоды в Стамбуле на 3 дня",
            reply_markup=get_persistent_keyboard()
        )
        return
    elif text == "🗺 Маршруты":
        # Показываем inline-меню выбора длительности маршрута
        keyboard = [
            [
                InlineKeyboardButton("1 день", callback_data="route_1"),
                InlineKeyboardButton("2 дня", callback_data="route_2"),
                InlineKeyboardButton("3 дня", callback_data="route_3"),
            ],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Выберите длительность маршрута:",
            reply_markup=reply_markup,
        )
        return
    elif text == "🏛 Достопримечательности":
        # Показываем inline-кнопки с районами
        reply_markup = create_district_keyboard("sights")
        await update.message.reply_text(
            "Выберите район для просмотра достопримечательностей:",
            reply_markup=reply_markup
        )
        return
    elif text == "🍽 Рестораны":
        # Показываем inline-кнопки с районами
        reply_markup = create_district_keyboard("restaurants")
        await update.message.reply_text(
            "Выберите район для просмотра ресторанов:",
            reply_markup=reply_markup
        )
        return

    # Проверка на запрос погоды
    # Улучшенное регулярное выражение: захватываем весь город до конца строки или до знаков препинания
    match = re.search(r'(температура|погода|прогноз).*?(?:в|по|для)\s+([а-яА-Яa-zA-ZёЁ\- ]+?)(?:\s|$|,|\.|\?|!|на\s+3|на\s+три)', text, re.IGNORECASE)
    if match:
        raw_city = match.group(2).strip()
        # Убираем лишние слова в конце
        raw_city = re.sub(r'\s+(сегодня|завтра|сейчас|погода|температура|прогноз).*$', '', raw_city, flags=re.IGNORECASE)
        # Нормализуем город (приводим к именительному падежу)
        city = normalize_city(raw_city)
        logger.info(f"WEATHER_REQUEST: raw_city={raw_city}, normalized_city={city}")
        if re.search(r'(на\s*3\s*дня|на\s*три\s*дня|прогноз)', text, re.IGNORECASE):
            forecast = await get_weather_forecast(city, days=3)
            if forecast:
                await update.message.reply_text(f"Прогноз погоды в {city} на ближайшие 3 дня:\n{forecast}")
            else:
                await update.message.reply_text("Не удалось получить прогноз погоды. Проверьте название города.")
            return
        weather = await get_weather(city)
        if weather:
            await update.message.reply_text(weather)
        else:
            await update.message.reply_text("Не удалось получить данные о погоде. Проверьте название города.")
        return

    # Для свободных вопросов используем двухшаговую схему:
    # 1. Кодом выбираем релевантные элементы из базы
    # 2. Передаём LLM ТОЛЬКО выбранные элементы для форматирования
    
    istanbul_keywords = ["стамбул", "istanbul", "гид по стамбулу", "маршрут", "турция", "что посмотреть"]
    is_istanbul_related = any(kw in text.lower() for kw in istanbul_keywords)
    
    # Если вопрос о Стамбуле, пытаемся найти релевантные данные в базе
    relevant_data = None
    if is_istanbul_related:
        # Ищем упоминания районов
        districts_mentioned = []
        for district in get_districts_from_data("sights") + get_districts_from_data("restaurants"):
            if district.lower() in text.lower():
                districts_mentioned.append(district)
        
        # Если упоминается район, собираем данные из базы
        if districts_mentioned:
            relevant_sights = []
            relevant_restaurants = []
            for district in districts_mentioned:
                relevant_sights.extend([
                    s for s in ISTANBUL_DATA.get("sights", [])
                    if normalize_district(s.get("district", "")).lower() == district.lower()
                ])
                relevant_restaurants.extend([
                    r for r in ISTANBUL_DATA.get("restaurants", [])
                    if normalize_district(r.get("district", "")).lower() == district.lower()
                ])
            
            if relevant_sights or relevant_restaurants:
                relevant_data = {
                    "sights": relevant_sights[:5],  # Ограничиваем количество
                    "restaurants": relevant_restaurants[:5],
                    "districts": districts_mentioned
                }
    
    # Обновлённый промпт с политикой "никаких фактов из головы"
    if is_istanbul_related:
        if relevant_data:
            # Передаём LLM только данные из базы для форматирования
            data_summary = []
            if relevant_data["sights"]:
                data_summary.append("Достопримечательности:")
                for s in relevant_data["sights"]:
                    data_summary.append(f"- {s.get('name', '')}: {s.get('description', '')}")
            if relevant_data["restaurants"]:
                data_summary.append("\nРестораны:")
                for r in relevant_data["restaurants"]:
                    data_summary.append(f"- {r.get('name', '')}: {r.get('cuisine', '')} ({r.get('price_level', '')})")
            
            PROMPT = f"""Ты — Telegram-бот-гид по Стамбулу. Пользователь задал вопрос о районе(ах): {', '.join(relevant_data['districts'])}.

ВАЖНО: Используй ТОЛЬКО данные из базы ниже. НЕ выдумывай факты, места, цены или часы работы, которых нет в базе.

Данные из базы:
{chr(10).join(data_summary)}

Отвечай кратко, структурированно, используя ТОЛЬКО информацию из базы. Если данных недостаточно для ответа, так и скажи."""
        else:
            PROMPT = """Ты — Telegram-бот-гид по Стамбулу. 

ВАЖНО: Если у тебя нет точных данных из базы, НЕ выдумывай факты. Лучше скажи, что нужно уточнить информацию или использовать кнопки меню для просмотра маршрутов, достопримечательностей и ресторанов из базы данных.

Отвечай кратко и честно."""
    else:
        PROMPT = DEFAULT_PROMPT_TEMPLATE

    user_histories[user_id].append({"role": "user", "content": text})
    # Берём только последние 4 сообщения истории (2 вопроса и 2 ответа)
    short_history = list(user_histories[user_id])[-4:]
    messages = [{"role": "system", "content": PROMPT}] + short_history

    try:
        response = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=600
        )

        if not response.choices:
            raise ValueError("GPT не вернул ответ")

        answer = response.choices[0].message.content.strip()

        # Защита от повторов: сравниваем с последним отправленным ответом ассистента
        last_assistant_msg = next((m["content"].strip() for m in reversed(user_histories[user_id]) if m["role"] == "assistant"), None)
        if last_assistant_msg and answer == last_assistant_msg:
            logger.warning("Ответ дублируется, не отправляется повторно.")
            return

        user_histories[user_id].append({"role": "assistant", "content": answer})

        # Форматируем и отправляем ответ в Markdown
        formatted_answer = format_markdown(answer)
        for part in split_into_telegram_chunks(formatted_answer, limit=3500):
            await update.message.reply_text(
                part,
                parse_mode='Markdown',
                disable_web_page_preview=True
            )

    except Exception as e:
        logger.error(f"Ошибка при обращении к GPT: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Произошла ошибка при обработке запроса.")

async def route_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Ищем маршрут на 1 день
    route = None
    for r in ISTANBUL_DATA.get("routes", []):
        title = r.get("title", "").lower()
        if "1 день" in title or "один день" in title or "классика" in title:
            route = r
            break
    
    if not route:
        await update.message.reply_text("Маршрут на 1 день не найден в базе данных.")
        return

    # Форматируем в Markdown
    lines = [f"**{route['title']}**"]
    for block in route.get("steps", []):
        time_block = block.get("time", "")
        lines.append(f"\n**{time_block}**")
        for act in block.get("activities", []):
            lines.append(f"• {act}")
    text = format_markdown("\n".join(lines))

    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def sights_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        districts = get_districts_from_data("sights")
        districts_list = "\n".join([f"• {d}" for d in districts])
        await update.message.reply_text(
            f"Укажите район: /sights <район>\n\n"
            f"Доступные районы:\n{districts_list}"
        )
        return
    
    district = normalize_district(" ".join(args))
    results = [
        sight for sight in ISTANBUL_DATA.get("sights", [])
        if normalize_district(sight.get("district", "")).lower() == district.lower()
    ]

    if not results:
        districts = get_districts_from_data("sights")
        districts_list = "\n".join([f"• {d}" for d in districts])
        await update.message.reply_text(
            f"В районе {district} ничего не найдено в базе данных.\n\n"
            f"Попробуйте другой район из списка:\n{districts_list}"
        )
        return

    # Форматируем в Markdown
    lines = [f"**🏛 Достопримечательности в районе {district}:**\n"]
    for s in results:
        name = s.get("name", "Без названия")
        desc = s.get("description", "")
        hours = s.get("opening_hours", "Уточняйте на месте")
        price = s.get("price", "Уточняйте на месте")
        transport = s.get("transport", "")
        
        lines.append(f"• **{name}**")
        if desc:
            lines.append(f"  {desc}")
        lines.append(f"  🕒 {hours}")
        lines.append(f"  💰 {price}")
        if transport:
            lines.append(f"  🚇 {transport}")
        lines.append("")
    
    text = format_markdown("\n".join(lines))

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

async def eat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        districts = get_districts_from_data("restaurants")
        districts_list = "\n".join([f"• {d}" for d in districts])
        await update.message.reply_text(
            f"Укажите район: /eat <район>\n\n"
            f"Доступные районы:\n{districts_list}"
        )
        return
    
    district = normalize_district(" ".join(args))
    results = [
        restaurant for restaurant in ISTANBUL_DATA.get("restaurants", [])
        if normalize_district(restaurant.get("district", "")).lower() == district.lower()
    ]

    if not results:
        districts = get_districts_from_data("restaurants")
        districts_list = "\n".join([f"• {d}" for d in districts])
        await update.message.reply_text(
            f"В районе {district} ничего не найдено в базе данных.\n\n"
            f"Попробуйте другой район из списка:\n{districts_list}"
        )
        return

    # Форматируем в Markdown
    lines = [f"**🍽 Рестораны в районе {district}:**\n"]
    for r in results:
        name = r.get("name", "Без названия")
        cuisine = r.get("cuisine", "")
        price = r.get("price_level", "")
        desc = r.get("description", "")
        hours = r.get("opening_hours", "Уточняйте на месте")
        address = r.get("address", "")
        transport = r.get("transport", "")
        tags = r.get("tags", [])
        
        lines.append(f"• **{name}**")
        if cuisine:
            lines.append(f"  🍳 {cuisine}")
        if price:
            lines.append(f"  💰 {price}")
        if desc:
            lines.append(f"  {desc}")
        lines.append(f"  🕒 {hours}")
        if address:
            lines.append(f"  📍 {address}")
        if transport:
            lines.append(f"  🚇 {transport}")
        if tags:
            lines.append(f"  {' '.join(['#' + tag.replace(' ', '_') for tag in tags])}")
        lines.append("")
    
    text = format_markdown("\n".join(lines))

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

def normalize_district(district: str) -> str:
    """Нормализует название района: убирает лишние пробелы, приводит к единому регистру."""
    if not district:
        return ""
    return " ".join(district.strip().split()).title()

def adapt_place_v2_to_legacy(place: dict) -> dict:
    """Адаптирует место из формата v2 к legacy формату."""
    adapted = {
        "name": place.get("name_ru", place.get("name", "")),
        "district": normalize_district(place.get("district", "")),
        "description": " ".join(place.get("highlights", [])) or place.get("description", ""),
        "opening_hours": place.get("visiting", {}).get("hours", "Уточняйте на месте"),
        "price": "Бесплатно" if place.get("category") == "мечеть" else "Уточняйте на месте",
        "transport": f"Район {place.get('district', '')}"
    }
    # Добавляем area если есть
    if "area" in place:
        adapted["area"] = place["area"]
    return adapted

def adapt_food_v2_to_legacy(food: dict) -> dict:
    """Адаптирует ресторан из формата v2 к legacy формату."""
    adapted = {
        "name": food.get("name", ""),
        "district": normalize_district(food.get("district", "")),
        "cuisine": food.get("cuisine", "турецкая"),
        "price_level": food.get("price_level", "₺"),
        "description": food.get("description", ""),
        "opening_hours": food.get("opening_hours", "Уточняйте на месте"),
        "address": food.get("address", ""),
        "transport": f"Район {food.get('district', '')}",
        "tags": food.get("tags", [])
    }
    return adapted

def adapt_route_v2_to_legacy(route_template: dict, places_map: dict, food_map: dict) -> dict:
    """Адаптирует маршрут из формата v2 к legacy формату с географической фильтрацией."""
    title = route_template.get("title", "")
    steps = []
    
    for step in route_template.get("steps", []):
        time_block = step.get("time_block", "")
        day = step.get("day", "")
        if day:
            time_block = f"День {day} — {time_block}"
        
        activities = []
        
        # Собираем районы мест в этом шаге для географической проверки
        step_districts = set()
        step_areas = set()
        
        # Добавляем места
        for stop_id in step.get("stop_ids", []):
            place = places_map.get(stop_id)
            if place:
                name = place.get("name_ru", place.get("name", stop_id))
                activities.append(f"Посещение {name}")
                # Собираем информацию о районе
                district = normalize_district(place.get("district", ""))
                area = place.get("area", "")
                if district:
                    step_districts.add(district)
                if area:
                    step_areas.add(area)
        
        # Добавляем еду с проверкой географической логичности
        for food_id in step.get("food_ids", []):
            food = food_map.get(food_id)
            if food:
                name = food.get("name", food_id)
                food_district = normalize_district(food.get("district", ""))
                
                # Проверяем, находится ли ресторан в том же районе
                if step_districts and food_district:
                    if food_district.lower() not in [d.lower() for d in step_districts]:
                        # Ресторан в другом районе - добавляем пометку
                        activities.append(f"🍽 Обед/ужин в {name} (нужно проехать в район {food_district})")
                    else:
                        # Ресторан в том же районе
                        activities.append(f"🍽 Обед/ужин в {name}")
                else:
                    # Если не можем определить район, просто добавляем название
                    activities.append(f"🍽 Обед/ужин в {name}")
        
        # Добавляем заметки
        for note in step.get("notes", []):
            activities.append(f"💡 {note}")
        
        if activities:
            steps.append({
                "time": time_block,
                "activities": activities
            })
    
    return {
        "title": title,
        "steps": steps
    }

def validate_and_normalize_data(data: dict, is_v2: bool) -> dict:
    """Валидирует и нормализует данные базы."""
    errors = []
    normalized = {}
    
    if is_v2:
        # Обработка формата v2
        places = data.get("places", [])
        food = data.get("food", [])
        route_templates = data.get("route_templates", [])
        
        # Создаём карты для быстрого доступа
        places_map = {p.get("id"): p for p in places}
        food_map = {f.get("id"): f for f in food}
        
        # Валидация и адаптация мест
        normalized_sights = []
        for place in places:
            if not place.get("name_ru") and not place.get("name"):
                errors.append(f"Место без названия: {place.get('id', 'unknown')}")
                continue
            if not place.get("district"):
                errors.append(f"Место без района: {place.get('name_ru', place.get('name', 'unknown'))}")
                continue
            normalized_sights.append(adapt_place_v2_to_legacy(place))
        normalized["sights"] = normalized_sights
        
        # Валидация и адаптация ресторанов
        normalized_restaurants = []
        for f in food:
            if not f.get("name"):
                errors.append(f"Ресторан без названия: {f.get('id', 'unknown')}")
                continue
            if not f.get("district"):
                errors.append(f"Ресторан без района: {f.get('name', 'unknown')}")
                continue
            normalized_restaurants.append(adapt_food_v2_to_legacy(f))
        normalized["restaurants"] = normalized_restaurants
        
        # Адаптация маршрутов
        normalized_routes = []
        for route_template in route_templates:
            try:
                adapted_route = adapt_route_v2_to_legacy(route_template, places_map, food_map)
                if adapted_route.get("steps"):
                    normalized_routes.append(adapted_route)
            except Exception as e:
                errors.append(f"Ошибка адаптации маршрута {route_template.get('id', 'unknown')}: {e}")
        normalized["routes"] = normalized_routes
        
    else:
        # Обработка legacy формата
        sights = data.get("sights", [])
        restaurants = data.get("restaurants", [])
        routes = data.get("routes", [])
        
        # Валидация и нормализация sights
        normalized_sights = []
        for sight in sights:
            if not sight.get("name"):
                errors.append(f"Достопримечательность без названия")
                continue
            if not sight.get("district"):
                errors.append(f"Достопримечательность без района: {sight.get('name', 'unknown')}")
                continue
            # Нормализуем district
            sight["district"] = normalize_district(sight["district"])
            # Проверяем наличие обязательных полей
            if "opening_hours" not in sight:
                sight["opening_hours"] = sight.get("open_hours", "Уточняйте на месте")
            if "price" not in sight:
                sight["price"] = "Уточняйте на месте"
            if "transport" not in sight:
                sight["transport"] = f"Район {sight['district']}"
            normalized_sights.append(sight)
        normalized["sights"] = normalized_sights
        
        # Валидация и нормализация restaurants
        normalized_restaurants = []
        for restaurant in restaurants:
            if not restaurant.get("name"):
                errors.append(f"Ресторан без названия")
                continue
            if not restaurant.get("district"):
                errors.append(f"Ресторан без района: {restaurant.get('name', 'unknown')}")
                continue
            restaurant["district"] = normalize_district(restaurant["district"])
            if "opening_hours" not in restaurant:
                restaurant["opening_hours"] = "Уточняйте на месте"
            if "address" not in restaurant:
                restaurant["address"] = ""
            if "transport" not in restaurant:
                restaurant["transport"] = f"Район {restaurant['district']}"
            if "tags" not in restaurant:
                restaurant["tags"] = []
            normalized_restaurants.append(restaurant)
        normalized["restaurants"] = normalized_restaurants
        
        # Валидация routes
        normalized_routes = []
        for route in routes:
            if not route.get("title"):
                errors.append("Маршрут без названия")
                continue
            if not route.get("steps"):
                errors.append(f"Маршрут без шагов: {route.get('title', 'unknown')}")
                continue
            normalized_routes.append(route)
        normalized["routes"] = normalized_routes
    
    # Логируем ошибки
    if errors:
        logger.warning(f"Найдено {len(errors)} ошибок при валидации базы:")
        for error in errors[:10]:  # Показываем первые 10
            logger.warning(f"  - {error}")
        if len(errors) > 10:
            logger.warning(f"  ... и ещё {len(errors) - 10} ошибок")
    
    return normalized

def load_istanbul_data():
    """Загружает базу данных Стамбула с поддержкой обоих форматов."""
    global ISTANBUL_DATA
    
    # Пробуем загрузить v2 формат
    v2_path = "istanbul_db_v2.yaml"
    legacy_path = "istanbul_guide.yaml"
    
    try:
        if os.path.exists(v2_path):
            logger.info(f"Загрузка базы данных из {v2_path}...")
            with open(v2_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            ISTANBUL_DATA = validate_and_normalize_data(data, is_v2=True)
            logger.info(f"База v2 загружена: {len(ISTANBUL_DATA.get('sights', []))} мест, "
                       f"{len(ISTANBUL_DATA.get('restaurants', []))} ресторанов, "
                       f"{len(ISTANBUL_DATA.get('routes', []))} маршрутов")
        elif os.path.exists(legacy_path):
            logger.info(f"Загрузка базы данных из {legacy_path}...")
            with open(legacy_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            ISTANBUL_DATA = validate_and_normalize_data(data, is_v2=False)
            logger.info(f"База legacy загружена: {len(ISTANBUL_DATA.get('sights', []))} мест, "
                       f"{len(ISTANBUL_DATA.get('restaurants', []))} ресторанов, "
                       f"{len(ISTANBUL_DATA.get('routes', []))} маршрутов")
        else:
            logger.error(f"Файлы базы данных не найдены: {v2_path}, {legacy_path}")
            ISTANBUL_DATA = {}
            return
        
        # Проверяем, что база не пустая
        if not ISTANBUL_DATA.get("sights") and not ISTANBUL_DATA.get("restaurants"):
            logger.error("База данных пуста или некорректна!")
            ISTANBUL_DATA = {}
    except FileNotFoundError as e:
        logger.error(f"Файл базы данных не найден: {e}")
        ISTANBUL_DATA = {}
    except Exception as e:
        logger.error(f"Ошибка при загрузке базы данных: {e}", exc_info=True)
        ISTANBUL_DATA = {}

def main() -> None:
    # Загружаем базу данных при старте
    load_istanbul_data()

    app = ApplicationBuilder()\
        .token(TOKEN)\
        .build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("route", route_command))
    app.add_handler(CommandHandler("sights", sights_command))
    app.add_handler(CommandHandler("eat", eat_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # <-- вот здесь сбрасываем pending updates
    app.run_polling(drop_pending_updates=True)



if __name__ == '__main__':
    main()

