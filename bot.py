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

def format_gpt_answer(text):
    """
    Форматирует ответ бота, добавляя HTML-разметку для режима "Гид по Стамбулу"
    и Markdown для обычных ответов.
    """
    # Удаляем лишние пробелы и переносы строк
    text = re.sub(r'\n{3,}', '\n\n', text.strip())
    # Жирные нумерованные заголовки вида "1. Утро"
    text = re.sub(r'^(\s*\d+\.\s+[^\n]+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    
    # Форматируем советы
    text = re.sub(r'💡\s+Совет:\s+([^\n]+)', r'💡 <b>Совет:</b> \1', text)
    
    # Добавляем эмодзи для разделов
    text = re.sub(r'<b>1\.\s+Основные достопримечательности</b>', 
                 r'<b>1. 🏛 Основные достопримечательности</b>', text)
    text = re.sub(r'<b>2\.\s+Что посмотреть в первую очередь</b>', 
                 r'<b>2. 🗺 Что посмотреть в первую очередь</b>', text)
    text = re.sub(r'<b>3\.\s+Вкусная и недорогая еда</b>', 
                 r'<b>3. 🍽 Вкусная и недорогая еда</b>', text)
    text = re.sub(r'<b>4\.\s+Полезные советы</b>', 
                 r'<b>4. 💡 Полезные советы</b>', text)
    
    return text

def fix_markdown(text):
    # Закрыть незакрытые **
    if text.count('**') % 2 != 0:
        text += '**'
    # Удалить лишние одинарные * внутри текста (оставить только в начале подпунктов)
    # Теперь подпункты — только нумерация, * не должно быть
    lines = text.split('\n')
    fixed_lines = []
    for line in lines:
        # Не трогаем строки с жирным пунктом
        if re.match(r'^\*\*\d+\. ', line):
            fixed_lines.append(line)
        else:
            fixed_lines.append(line.replace('*', ''))
    return '\n'.join(fixed_lines)

def clean_answer(text):
    import re
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Жирный текст: **текст** → <b>текст</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
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
        keyboard = [
            [
                InlineKeyboardButton("Султанахмет", callback_data="sights_Султанахмет"),
                InlineKeyboardButton("Галата", callback_data="sights_Галата")
            ],
            [
                InlineKeyboardButton("Беязит", callback_data="sights_Беязит"),
                InlineKeyboardButton("Бешикташ", callback_data="sights_Бешикташ")
            ],
            [
                InlineKeyboardButton("Вефа", callback_data="sights_Вефа"),
                InlineKeyboardButton("Эминоню", callback_data="sights_Эминоню")
            ],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "Выберите район для просмотра достопримечательностей:",
            reply_markup=reply_markup
        )

    elif query.data == "restaurants":
        keyboard = [
            [
                InlineKeyboardButton("Султанахмет", callback_data="eat_Султанахмет"),
                InlineKeyboardButton("Бейоглу", callback_data="eat_Бейоглу")
            ],
            [
                InlineKeyboardButton("Каракёй", callback_data="eat_Каракёй"),
                InlineKeyboardButton("Эминоню", callback_data="eat_Эминоню")
            ],
            [
                InlineKeyboardButton("Кадыкёй", callback_data="eat_Кадыкёй"),
                InlineKeyboardButton("Нишанташи", callback_data="eat_Нишанташи")
            ],
            [
                InlineKeyboardButton("Бешикташ", callback_data="eat_Бешикташ")
            ],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
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
        route = next((r for r in ISTANBUL_DATA.get("routes", []) 
                     if r["title"] == f"Маршрут на {days} {'день' if days == '1' else 'дня' if days == '2' else 'дня'} по Стамбулу"), None)
        
        if not route:
            await query.edit_message_text("Маршрут не найден.")
            return

        lines = [f"<b>{route['title']}</b>"]
        for block in route["steps"]:
            lines.append(f"\n<b>{block['time']}:</b>")
            for act in block["activities"]:
                lines.append(f"• {act}")
        text = "\n".join(lines)

        keyboard = [[InlineKeyboardButton("◀️ Назад к маршрутам", callback_data="routes")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")

    elif query.data.startswith("sights_"):
        district = query.data.split("_")[1]
        results = [
            sight for sight in ISTANBUL_DATA.get("sights", [])
            if sight["district"].lower() == district.lower()
        ]

        if not results:
            await query.edit_message_text(f"В районе {district} ничего не найдено.")
            return

        lines = [f"<b>🏛 Достопримечательности в районе {district}:</b>\n"]
        for s in results:
            lines.append(
                f"• <b>{s['name']}</b>\n"
                f"  {s['description']}\n"
                f"  🕒 {s['opening_hours']}\n"
                f"  💰 {s['price']}\n"
                f"  🚇 {s['transport']}\n"
            )

        keyboard = [[InlineKeyboardButton("◀️ Назад к районам", callback_data="sights")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    elif query.data.startswith("eat_"):
        district = query.data.split("_")[1]
        results = [
            restaurant for restaurant in ISTANBUL_DATA.get("restaurants", [])
            if restaurant["district"].lower() == district.lower()
        ]

        if not results:
            await query.edit_message_text(f"В районе {district} ничего не найдено.")
            return

        lines = [f"<b>🍽 Рестораны в районе {district}:</b>\n"]
        for r in results:
            lines.append(
                f"• <b>{r['name']}</b>\n"
                f"  🍳 {r['cuisine']}\n"
                f"  💰 {r['price_level']}\n"
                f"  {r['description']}\n"
                f"  🕒 {r['opening_hours']}\n"
                f"  📍 {r['address']}\n"
                f"  🚇 {r['transport']}\n"
                f"  #{' #'.join(r['tags'])}\n"
            )

        keyboard = [[InlineKeyboardButton("◀️ Назад к районам", callback_data="restaurants")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=reply_markup,
            parse_mode="HTML",
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
        await update.message.reply_text(
            "Укажите район: Султанахмет, Галата, Беязит, Бешикташ, Вефа, Эминоню",
            reply_markup=get_persistent_keyboard()
        )
        return
    elif text == "🍽 Рестораны":
        await update.message.reply_text(
            "Укажите район: Султанахмет, Бейоглу, Каракёй, Эминоню, Кадыкёй, Нишанташи, Бешикташ",
            reply_markup=get_persistent_keyboard()
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

    # --- Определение промпта по ключевым словам ---
    istanbul_keywords = ["стамбул", "istanbul", "гид по стамбулу", "маршрут", "турция", "что посмотреть"]
    is_istanbul_related = any(kw in text.lower() for kw in istanbul_keywords)
    
    # Определяем, когда пользователь явно просит маршрут на 1 день
    is_route_request = any(kw in text.lower() for kw in ["маршрут", "на день", "за 1 день", "в один день", "что успеть", "однодневный маршрут"])

    if "стамбул" in text.lower() and is_route_request:
        PROMPT = PROMPT_TEMPLATE_ROUTE
    elif is_istanbul_related:
        PROMPT = PROMPT_TEMPLATE_ISTANBUL
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

        # Форматируем и отправляем ответ частями, чтобы Telegram не обрезал
        if is_istanbul_related:
            formatted_answer = format_gpt_answer(answer)
            for part in split_into_telegram_chunks(formatted_answer, limit=3500):
                await update.message.reply_text(
                    part,
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
        else:
            # Очищаем от проблемных Markdown-символов для обычных ответов
            cleaned_answer = clean_markdown_formatting(answer)
            for part in split_into_telegram_chunks(cleaned_answer, limit=3500):
                await update.message.reply_text(
                    part,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )

    except Exception as e:
        logger.error(f"Ошибка при обращении к GPT: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Произошла ошибка при обработке запроса.")

async def route_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = "Маршрут на 1 день по Стамбулу"
    route = next((r for r in ISTANBUL_DATA.get("routes", []) if r["title"] == title), None)

    if not route:
        await update.message.reply_text("Извините, маршрут не найден.")
        return

    lines = [f"<b>{title}</b>"]
    for block in route["steps"]:
        lines.append(f"\n<b>{block['time']}:</b>")
        for act in block["activities"]:
            lines.append(f"• {act}")
    text = "\n".join(lines)

    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def sights_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Укажите район: /sights Султанахмет\n\n"
            "Доступные районы:\n"
            "• Султанахмет\n"
            "• Галата\n"
            "• Беязит\n"
            "• Бешикташ\n"
            "• Вефа\n"
            "• Эминоню"
        )
        return
    
    district = " ".join(args).strip().lower()
    results = [
        sight for sight in ISTANBUL_DATA.get("sights", [])
        if sight["district"].lower() == district
    ]

    if not results:
        await update.message.reply_text(
            f"В районе {district.title()} ничего не найдено.\n"
            "Попробуйте другой район из списка:\n"
            "• Султанахмет\n"
            "• Галата\n"
            "• Беязит\n"
            "• Бешикташ\n"
            "• Вефа\n"
            "• Эминоню"
        )
        return

    lines = [f"<b>🏛 Достопримечательности в районе {district.title()}:</b>\n"]
    for s in results:
        lines.append(
            f"• <b>{s['name']}</b>\n"
            f"  {s['description']}\n"
            f"  🕒 {s['opening_hours']}\n"
            f"  💰 {s['price']}\n"
            f"  🚇 {s['transport']}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True
    )

async def eat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Укажите район: /eat Султанахмет\n\n"
            "Доступные районы:\n"
            "• Султанахмет\n"
            "• Бейоглу\n"
            "• Каракёй\n"
            "• Эминоню\n"
            "• Кадыкёй\n"
            "• Нишанташи\n"
            "• Бешикташ"
        )
        return
    
    district = " ".join(args).strip().lower()
    results = [
        restaurant for restaurant in ISTANBUL_DATA.get("restaurants", [])
        if restaurant["district"].lower() == district
    ]

    if not results:
        await update.message.reply_text(
            f"В районе {district.title()} ничего не найдено.\n"
            "Попробуйте другой район из списка:\n"
            "• Султанахмет\n"
            "• Бейоглу\n"
            "• Каракёй\n"
            "• Эминоню\n"
            "• Кадыкёй\n"
            "• Нишанташи\n"
            "• Бешикташ"
        )
        return

    lines = [f"<b>🍽 Рестораны в районе {district.title()}:</b>\n"]
    for r in results:
        lines.append(
            f"• <b>{r['name']}</b>\n"
            f"  🍳 {r['cuisine']}\n"
            f"  💰 {r['price_level']}\n"
            f"  {r['description']}\n"
            f"  🕒 {r['opening_hours']}\n"
            f"  📍 {r['address']}\n"
            f"  🚇 {r['transport']}\n"
            f"  #{' #'.join(r['tags'])}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True
    )

def load_istanbul_data():
    global ISTANBUL_DATA
    try:
        logger.info("Начинаю загрузку базы данных Стамбула...")
        file_path = "istanbul_guide.yaml"
        logger.info(f"Путь к файлу: {os.path.abspath(file_path)}")
        with open(file_path, "r", encoding="utf-8") as f:
            ISTANBUL_DATA = yaml.safe_load(f)
            logger.info(f"База данных успешно загружена. Количество маршрутов: {len(ISTANBUL_DATA.get('routes', []))}")
            logger.info(f"Доступные маршруты: {[r['title'] for r in ISTANBUL_DATA.get('routes', [])]}")
    except FileNotFoundError:
        logger.error(f"Файл {file_path} не найден!")
        ISTANBUL_DATA = {}
    except Exception as e:
        logger.error(f"Ошибка при загрузке базы данных: {e}")
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

