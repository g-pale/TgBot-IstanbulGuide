import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY')

# Настройки истории
HISTORY_MAXLEN = 10

# Настройки chunk_text
CHUNK_MAX_LEN = 1000

# URL для OpenRouter и таймауты
OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'
HTTP_TIMEOUT = 5