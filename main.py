import nest_asyncio
import time
import re
import csv
import os
import json
import asyncio
import aiohttp
import logging
from datetime import datetime, timedelta
from functools import lru_cache
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters
)
import config

# ----------------- Setup Logging -----------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(config.CONST_PATH, "app.log")),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# ----------------- Cache Configuration -----------------

# Cache for storing results to avoid repeated API calls
CACHE_EXPIRY = timedelta(hours=24)
price_cache = {}

def get_from_cache(app_id, store_type):
    """Get cached result if available and not expired"""
    cache_key = f"{app_id}_{store_type}"
    if cache_key in price_cache:
        timestamp, filepath = price_cache[cache_key]
        if datetime.now() - timestamp < CACHE_EXPIRY and os.path.exists(filepath):
            logger.info(f"Cache hit for {cache_key}")
            return filepath
    return None

def save_to_cache(app_id, store_type, filepath):
    """Save result to cache"""
    cache_key = f"{app_id}_{store_type}"
    price_cache[cache_key] = (datetime.now(), filepath)
    logger.info(f"Saved to cache: {cache_key}")

# ----------------- Country and Currency Data -----------------

country_currency_dict = {
    "DZ": "DZD", "AU": "AUD", "BH": "BHD", "BD": "BDT", "BO": "BOB", "BR": "BRL",
    "KH": "KHR", "CA": "CAD", "KYD": "KYD", "CL": "CLP", "CO": "COP", "CR": "CRC",
    "EG": "EGP", "GE": "GEL", "GH": "GHS", "HK": "HKD", "IN": "INR", "ID": "IDR",
    "IQ": "IQD", "IL": "ILS", "JP": "JPY", "JO": "JOD", "KZ": "KZT", "KE": "KES",
    "KR": "KRW", "KW": "KWD", "MO": "MOP", "MY": "MYR", "MX": "MXN", "MA": "MAD",
    "MM": "MMK", "NZ": "NZD", "NG": "NGN", "OM": "OMR", "PK": "PKR", "PA": "PAB",
    "PY": "PYG", "PE": "PEN", "PH": "PHP", "QA": "QAR", "RU": "RUB", "SA": "SAR",
    "RS": "RSD", "SG": "SGD", "ZA": "ZAR", "LK": "LKR", "TW": "TWD", "TZ": "TZS",
    "TH": "THB", "TR": "TRY", "UA": "UAH", "AE": "AED", "US": "USD", "VN": "VND",
}

# Countries to parse (can be adjusted based on importance)
countries = [
    "DZ","EG","AU","BD","BO","BR","CA","CL","CO","CR","GE","GH","HK","IN","ID","IQ",
    "IL","JP","JO","KZ","KE","KR","MO","MY","MX","MA","MM","NZ","NG","PK","PY","PE",
    "PH","QA","RU","SA","RS","SG","ZA","LK","TW","TZ","TH","TR","UA","AE","US","VN"
]

# Курс валюты к USD (примерные/условные значения)
currency_rates = {
    "DZD": 132.966, "AUD": 1.583982, "BHD": 0.376241, "BDT": 109.73,
    "BOB": 6.909550, "BRL": 5.806974, "CAD": 1.433827, "KYD": 0.833,
    "CLP": 961.794638, "COP": 4153.599492, "CRC": 504.817577, "EGP": 50.581311,
    "GEL": 2.867107, "GHS": 15.187930, "HKD": 7.787505, "INR": 86.249922,
    "IDR": 16149.393463, "IQD": 1309.703222, "ILS": 3.587793, "JPY": 155.855438,
    "JOD": 0.709118, "KZT": 505.503277, "KES": 129.264801, "KRW": 1432.185253,
    "KWD": 0.308060, "MOP": 8.021963, "MYR": 4.392292, "MXN": 20.245294,
    "MAD": 10.007902, "MMK": 2099.980901, "NZD": 1.752597, "NGN": 1550.620034,
    "OMR": 0.384454, "PKR": 278.655722, "PYG": 7918.619687, "PEN": 3.712514,
    "PHP": 58.388686, "QAR": 3.639992, "RUB": 97.929483, "SAR": 3.750482,
    "RSD": 112.125584, "SGD": 1.348339, "ZAR": 18.384263, "LKR": 298.761937,
    "TWD": 32.687009, "TZS": 2507.601986, "THB": 33.712166, "TRY": 35.678472,
    "UAH": 40.939132, "AED": 3.671703, "USD": 1,   "VND": 25094.287781
}

# ----------------- HTTP Request Utilities -----------------

async def get_with_retry(session, url, max_retries=3, backoff_factor=1.5, timeout=10):
    """Make HTTP GET with automatic retries and exponential backoff"""
    for attempt in range(max_retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status == 404:
                    logger.warning(f"404 Not Found: {url}")
                    return None, 404
                text = await response.text()
                return text, response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == max_retries - 1:  # last attempt
                logger.error(f"Failed after {max_retries} attempts: {url}, Error: {e}")
                return None, 0
            wait_time = backoff_factor * (2 ** attempt)
            logger.info(f"Retry in {wait_time:.1f}s due to {e} for URL: {url}")
            await asyncio.sleep(wait_time)

# ----------------- Rate Limiting -----------------

class RateLimiter:
    def __init__(self, rate=5, per=1):
        self.rate = rate  # operations per second
        self.per = per    # time period in seconds
        self.allowance = rate  # initial allowance
        self.last_check = time.time()
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            current = time.time()
            time_passed = current - self.last_check
            self.last_check = current
            self.allowance += time_passed * (self.rate / self.per)
            
            if self.allowance > self.rate:
                self.allowance = self.rate  # throttle
                
            if self.allowance < 1.0:
                wait_time = (1.0 - self.allowance) * self.per / self.rate
                logger.debug(f"Rate limiting: waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)
                self.allowance = 0.0
            else:
                self.allowance -= 1.0

# Create rate limiters for each service
google_limiter = RateLimiter(rate=10, per=1)  # 10 requests per second
apple_limiter = RateLimiter(rate=5, per=1)    # 5 requests per second

# ----------------- Google Play Price Parsing -----------------

# Cache the currency parsers to avoid rebuilding them
@lru_cache(maxsize=128)
def get_currency_parser(currency_code):
    """Return a parser function for the given currency code"""
    parsers = {
        'IDR': lambda x: float(x.replace('Rp ', '').replace('.', '').replace(',00', '')),
        'JOD': lambda x: float(x.replace('JOD ', '').replace('.000', '')),
        'TRY': lambda x: float(x.replace('TRY ', '').replace(',', '')),
        'JPY': lambda x: float(x.replace('¥', '').replace(',', '')),
        'KRW': lambda x: float(x.replace('₩', '').replace(',', '')),
        'INR': lambda x: float(x.replace('₹', '').replace(',', '')),
        'VND': lambda x: float(x.replace('₫', '').replace(',', '')),
        'HKD': lambda x: float(x.replace('HK$', '').replace(',', '')),
        'TWD': lambda x: float(x.replace('NT$', '').replace(',', '')),
        'USD': lambda x: float(x.replace('$', '')),
        'AUD': lambda x: float(x.replace('$', '')),
        'NZD': lambda x: float(x.replace('$', '')),
        'CAD': lambda x: float(x.replace('$', '')),
        'SGD': lambda x: float(x.replace('$', '')),
        'ILS': lambda x: float(x.replace('₪', '').replace(',', '')),
        'ZAR': lambda x: float(x.replace('R ', '').replace(' ', '').replace(',', '.')),
        # Default parser
        'DEFAULT': lambda x: float(re.search(r'[\d,.]+', x).group(0).replace(',', ''))
    }
    
    # Clean up whitespace and special characters
    def clean_string(x):
        return x.replace(' per item', '').replace('\xa0', ' ').strip()
    
    parser = parsers.get(currency_code, parsers['DEFAULT'])
    
    # Return a function that first cleans the string, then applies the specific parser
    return lambda x: parser(clean_string(x))

async def convert_price_to_usd_google(price_str, currency_code):
    """
    Converts price strings to USD range (min, max)
    """
    try:
        first_range = price_str.split(';')[0].strip()
        
        if '-' in first_range:
            min_price_str, max_price_str = [p.strip() for p in first_range.split('-')]
        else:
            min_price_str = max_price_str = first_range

        parser = get_currency_parser(currency_code)
        
        min_price = parser(min_price_str)
        max_price = parser(max_price_str)

        rate = currency_rates.get(currency_code, 1)
        min_usd = max(round(min_price / rate, 2), 0.01)
        max_usd = max(round(max_price / rate, 2), 0.01)

        return (min_usd, max_usd)
    except Exception as e:
        logger.error(f"Error parsing {currency_code}: {price_str}. Error: {e}")
        return (0.0, 0.0)

async def get_prices_for_country_google(session, country_code, app_id):
    """Fetch prices for a specific country from Google Play"""
    currency_code = country_currency_dict.get(country_code, "USD")
    url = f'https://play.google.com/store/apps/details?id={app_id}&hl=en&gl={country_code}'
    
    # Apply rate limiting
    await google_limiter.acquire()
    
    content, status = await get_with_retry(session, url)
    
    if not content:
        return None, currency_code, 'failed'
    
    if status == 404:
        logger.info(f"[Google] {country_code}: 404 Not Found")
        return None, currency_code, '404'
        
    logger.info(f"[Google] {country_code}: Processing response")
    
    if "In-app purchases" not in content:
        logger.info(f"[Google] {country_code}: No in-app purchases found")
        return None, currency_code, 'noinapp'
        
    # Search for price patterns
    matches = re.findall(r'"([^"]*?\sper\sitem)",', content)
    return matches, currency_code, True

async def fetch_prices_google(update, context, app_id):
    """Fetch prices from Google Play for all countries"""
    # Check cache first
    cached_path = get_from_cache(app_id, 'google')
    if cached_path:
        await update.message.reply_text('Возвращаем данные из кэша...')
        return cached_path
    
    await update.message.reply_text('Обработка для Google Play началась...')
    collected_data = []
    progress_message = await update.message.reply_text('Прогресс: 0%')
    
    # Use a single shared session for all requests
    async with aiohttp.ClientSession() as session:
        batch_size = 5  # Process 5 countries at a time
        total_batches = (len(countries) + batch_size - 1) // batch_size
        
        for i in range(0, len(countries), batch_size):
            batch = countries[i:i+batch_size]
            batch_num = i // batch_size + 1
            
            # Update progress
            progress_percent = min(100, int((batch_num / total_batches) * 100))
            await progress_message.edit_text(f'Прогресс: {progress_percent}% ({batch_num}/{total_batches} групп стран)')
            
            # Process batch
            tasks = [get_prices_for_country_google(session, cc, app_id) for cc in batch]
            batch_results = await asyncio.gather(*tasks)

            for j, result in enumerate(batch_results):
                prices, currency_code, success = result
                country_code = batch[j]

                if success is True and prices:
                    min_price_usd, max_price_usd = await convert_price_to_usd_google(prices[0], currency_code)
                    collected_data.append([
                        min_price_usd,
                        max_price_usd,
                        country_code,
                        currency_code,
                        prices[0]
                    ])
                elif success == '404':
                    logger.warning(f"{country_code}: Page not found (404).")
                elif success == 'timeout':
                    logger.warning(f"{country_code}: Request timed out.")
                else:
                    logger.info(f"{country_code}: No data found or error.")

    # Sort by Min Price
    sorted_data = sorted(collected_data, key=lambda x: float(x[0]))
    
    # Create unique filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(config.CONST_PATH, f"{app_id}_google_{timestamp}.csv")

    try:
        with open(filepath, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow(['Min Price (USD)', 'Max Price (USD)', 'Country', 'Currency', 'Original Price Range'])
            writer.writerows(sorted_data)
        
        # Save to cache
        save_to_cache(app_id, 'google', filepath)
        await progress_message.edit_text(f'Обработка для Google Play завершена! Найдено цен: {len(sorted_data)}')
    except Exception as e:
        logger.error(f"CSV write error: {e}")
        await update.message.reply_text(f"Ошибка при сохранении файла: {e}")

    return filepath

# ----------------- App Store Price Parsing -----------------

arabic_digits_map = {
    '٠': '0', '١': '1', '٢': '2', '٣': '3',
    '٤': '4', '٥': '5', '٦': '6', '٧': '7',
    '٨': '8', '٩': '9'
}

currency_configs = {
    "DZD": {
        "strip_strings": ["‏US", "US$"],
        "arabic_digits_map": None,
        "arabic_decimal_dot": None,
        "thousands_sep": None,
        "decimal_sep": ",",
        "is_already_usd": True
    },
    "BRL": {
        "strip_strings": ["R$"],
        "arabic_digits_map": None,
        "arabic_decimal_dot": None,
        "thousands_sep": ".",
        "decimal_sep": ",",
        "is_already_usd": False
    },
    "EGP": {
        "strip_strings": ["ج.م.‏"],
        "arabic_digits_map": arabic_digits_map,
        "arabic_decimal_dot": "٫",
        "thousands_sep": None,
        "decimal_sep": None,
        "is_already_usd": False
    },
    "COP": {
        "strip_strings": [],
        "arabic_digits_map": None,
        "arabic_decimal_dot": None,
        "thousands_sep": ".",
        "decimal_sep": ",",
        "is_already_usd": False
    },
    "CLP": {
        "strip_strings": [],
        "arabic_digits_map": None,
        "arabic_decimal_dot": None,
        "thousands_sep": ".",
        "decimal_sep": ",",
        "is_already_usd": False
    },
    "USD": {
        "strip_strings": [],
        "arabic_digits_map": None,
        "arabic_decimal_dot": None,
        "thousands_sep": None,
        "decimal_sep": None,
        "is_already_usd": True
    },
    "DEFAULT": {
        "strip_strings": [],
        "arabic_digits_map": None,
        "arabic_decimal_dot": None,
        "thousands_sep": None,
        "decimal_sep": None,
        "is_already_usd": False
    }
}

async def convert_price_to_usd_apple(price_str: str, currency_code: str):
    """
    Converts App Store prices to USD
    """
    try:
        if 'USD' in price_str.upper() or 'DZD' in price_str.upper():
            numeric_part = re.sub(r'[^0-9.,]+', '', price_str)
            numeric_part = numeric_part.replace(',', '')
            price_usd = float(numeric_part) if numeric_part else 0.0
            return (price_usd, price_usd)

        cfg = currency_configs.get(currency_code, currency_configs["DEFAULT"])

        for s in cfg["strip_strings"]:
            price_str = price_str.replace(s, "")

        if cfg["arabic_digits_map"]:
            if cfg["arabic_decimal_dot"]:
                price_str = price_str.replace(cfg["arabic_decimal_dot"], ".")
            converted = []
            for ch in price_str:
                if ch in cfg["arabic_digits_map"]:
                    converted.append(cfg["arabic_digits_map"][ch])
                else:
                    converted.append(ch)
            price_str = ''.join(converted)

        clean_str = re.sub(r'[^0-9.,]+', '', price_str)

        if cfg["thousands_sep"]:
            clean_str = clean_str.replace(cfg["thousands_sep"], '')

        if cfg["decimal_sep"] and cfg["decimal_sep"] != '.':
            clean_str = clean_str.replace(cfg["decimal_sep"], '.')

        numeric_price = float(clean_str) if clean_str else 0.0

        if cfg["is_already_usd"]:
            price_usd = numeric_price
        else:
            rate = currency_rates.get(currency_code, 1.0)
            price_usd = numeric_price / rate

        price_usd = max(round(price_usd, 2), 0.01)
        return (price_usd, price_usd)

    except Exception as e:
        logger.error(f"[Apple] Error parsing '{currency_code}': '{price_str}'. Error: {e}")
        return (0.0, 0.0)

async def get_prices_for_country_apple(session, country_code, apple_id):
    """Fetch App Store prices for a specific country"""
    url = f"https://app.sensortower.com/api/ios/apps/{apple_id}?country={country_code}"
    currency_code = country_currency_dict.get(country_code, "USD")
    
    # Apply rate limiting
    await apple_limiter.acquire()
    
    content, status = await get_with_retry(session, url)
    
    if not content:
        logger.warning(f"[Apple] {country_code}: Failed to get response")
        return None
        
    if status == 404:
        logger.warning(f"[Apple] {country_code}: 404 Not Found")
        return None
    
    try:
        data = json.loads(content)
        
        if "top_in_app_purchases" not in data:
            logger.info(f"[Apple] {country_code}: No top_in_app_purchases found")
            return None

        iaps_for_country = data["top_in_app_purchases"].get(country_code)
        if not iaps_for_country:
            logger.info(f"[Apple] {country_code}: No IAPs for this country")
            return None

        results = []
        for iap in iaps_for_country:
            price_str = iap.get("price", "")
            name = iap.get("name", "")
            duration = iap.get("duration", "")

            min_price_usd, max_price_usd = await convert_price_to_usd_apple(price_str, currency_code)

            results.append({
                "name": name,
                "price_str": price_str,
                "currency_code": currency_code,
                "duration": duration,
                "min_price_usd": min_price_usd,
                "max_price_usd": max_price_usd
            })
        
        logger.info(f"[Apple] {country_code}: Found {len(results)} IAPs")
        return results
        
    except json.JSONDecodeError as e:
        logger.error(f"[Apple] {country_code}: JSON decode error: {e}")
        return None
    except Exception as e:
        logger.error(f"[Apple] {country_code}: Error processing data: {e}")
        return None

async def fetch_prices_apple(update, context, apple_id):
    """Fetch App Store prices for all specified countries"""
    # Check cache first
    cached_path = get_from_cache(apple_id, 'apple')
    if cached_path:
        await update.message.reply_text('Возвращаем данные из кэша...')
        return cached_path
    
    await update.message.reply_text("Обработка для App Store (JSON API) началась...")
    collected_data = []
    
    # Using a shared session and processing multiple countries
    async with aiohttp.ClientSession() as session:
        # Expand to process more countries, not just Egypt
        apple_countries = ["EG", "US", "AU", "CA", "GB", "JP", "KR", "RU", "BR", "IN"]
        progress_message = await update.message.reply_text('Прогресс: 0%')
        
        for i, country_code in enumerate(apple_countries):
            # Update progress
            progress_percent = min(100, int(((i + 1) / len(apple_countries)) * 100))
            await progress_message.edit_text(f'Прогресс: {progress_percent}% ({i+1}/{len(apple_countries)} стран)')
            
            try:
                iaps_list = await get_prices_for_country_apple(session, country_code, apple_id)
                
                if iaps_list:
                    for iap in iaps_list:
                        collected_data.append([
                            iap["min_price_usd"],
                            iap["max_price_usd"],
                            country_code,
                            iap["currency_code"],
                            iap["price_str"],
                            iap["name"],
                            iap["duration"],
                        ])
                    logger.info(f"[Apple] {country_code}: Data found and processed.")
                else:
                    logger.info(f"[Apple] {country_code}: No data found or empty.")
            
            except Exception as e:
                logger.error(f"[Apple] {country_code} Error: {e}")
                
            # Small delay between countries
            await asyncio.sleep(0.5)

    sorted_data = sorted(collected_data, key=lambda x: float(x[0]))
    
    # Create unique filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(config.CONST_PATH, f"{apple_id}_apple_{timestamp}.csv")
    
    try:
        with open(filepath, mode='w', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            writer.writerow([
                'Min Price (USD)',
                'Max Price (USD)',
                'Country',
                'Currency',
                'Original Price',
                'IAP Name',
                'Duration'
            ])
            writer.writerows(sorted_data)
            
        # Save to cache
        save_to_cache(apple_id, 'apple', filepath)
        await progress_message.edit_text(f'Обработка для App Store завершена! Найдено цен: {len(sorted_data)}')
    except Exception as e:
        logger.error(f"CSV write error for Apple: {e}")
        await update.message.reply_text(f"Ошибка при сохранении файла: {e}")

    return filepath

# ----------------- User Management -----------------

# Optional: Whitelisted users (for access control)
WHITELISTED_USERS = set()  # Set this in config or leave empty for no restrictions

# Per-user rate limiting
user_request_times = {}

async def check_rate_limit(user_id, max_requests=5, period=60):
    """
    Limit users to max_requests per period (in seconds)
    Returns True if rate limit is not exceeded, False otherwise
    """
    current_time = time.time()
    
    if user_id not in user_request_times:
        user_request_times[user_id] = []
    
    # Remove old requests
    user_request_times[user_id] = [t for t in user_request_times[user_id] 
                                  if current_time - t < period]
    
    # Check if limit is exceeded
    if len(user_request_times[user_id]) >= max_requests:
        return False
    
    # Add current request
    user_request_times[user_id].append(current_time)
    return True

# ----------------- Telegram Bot Handlers -----------------

async def start(update, context):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "👋 Привет! Я бот для анализа цен приложений.\n\n"
            "Отправьте мне ссылку на приложение из:\n"
            "• Google Play (https://play.google.com/store/apps/details?id=xxx)\n"
            "• App Store (https://apps.apple.com/xx/app/yyy/idNNNN)\n\n"
            "Я проанализирую цены в разных странах и пришлю вам отчет в CSV формате."
        )
    )

async def help_command(update, context):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "📚 Справка по использованию бота:\n\n"
            "1️⃣ Отправьте ссылку на приложение из Google Play или App Store\n"
            "2️⃣ Дождитесь обработки (это может занять несколько минут)\n"
            "3️⃣ Получите CSV-файл с анализом цен\n\n"
            "Примеры ссылок:\n"
            "• https://play.google.com/store/apps/details?id=com.example.app\n"
            "• https://apps.apple.com/us/app/name/id123456789\n\n"
            "Для повторного запуска бота используйте команду /start"
        )
    )

async def status_command(update, context):
    cache_info = f"Размер кэша: {len(price_cache)} записей"
    
    user = update.effective_user
    user_id = user.id
    
    # Get rate limit info for this user
    current_time = time.time()
    recent_requests = 0
    if user_id in user_request_times:
        recent_requests = len([t for t in user_request_times[user_id] 
                             if current_time - t < 60])
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "📊 Статус системы:\n\n"
            f"• {cache_info}\n"
            f"• Ваши запросы: {recent_requests}/5 за последнюю минуту\n"
            f"• Время сервера: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    )

async def validate_app_url(url):
    """Validate URL format and extract app ID"""
    if "play.google.com" in url:
        match = re.search(r'id=([\w\d\.]+)', url)
        if match:
            return "google", match.group(1)
    elif "apps.apple.com" in url:
        match = re.search(r'/id(\d+)', url)
        if match:
            return "apple", match.group(1)
    return None, None

async def handle_message(update, context):
    user = update.effective_user
    user_id = user.id
    message = update.message.text.strip()

    # Проверка на превышение лимита запросов
    if not await check_rate_limit(user_id):
        await update.message.reply_text(
            "⏳ Вы превысили лимит запросов. Попробуйте снова через минуту."
        )
        return

    # Валидация ссылки
    store_type, app_id = await validate_app_url(message)
    if not app_id:
        await update.message.reply_text(
            "❌ Неверная ссылка. Пожалуйста, отправьте ссылку на приложение из Google Play или App Store."
        )
        return

    # Обработка ссылки в зависимости от магазина
    if store_type == "google":
        filepath = await fetch_prices_google(update, context, app_id)
    elif store_type == "apple":
        filepath = await fetch_prices_apple(update, context, app_id)
    else:
        await update.message.reply_text(
            "❌ Не удалось определить магазин. Пожалуйста, проверьте ссылку."
        )
        return

    # Отправка CSV-файла пользователю
    if filepath and os.path.exists(filepath):
        await update.message.reply_document(
            document=open(filepath, "rb"),
            filename=os.path.basename(filepath),
            caption="📄 Ваш отчет готов!"
        )
    else:
        await update.message.reply_text(
            "❌ Произошла ошибка при обработке. Попробуйте снова позже."
        )
if __name__ == "__main__":
    from telegram.ext import ApplicationBuilder

    # Создайте приложение Telegram
    application = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Добавьте обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Запустите бота
    application.run_polling()