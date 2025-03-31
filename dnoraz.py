import re
import traceback
import json
import sys
import sqlite3
import logging
import asyncio
import secrets
import string
import locale

import sys
import io

from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telegram.error import BadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import requests
from fastapi import FastAPI, Request

# Устанавливаем UTF-8 кодировку для всего скрипта
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
locale.setlocale(locale.LC_ALL, 'ru_RU.UTF-8')

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ],
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_USERNAME = 'OdnorazkiGash_bot'
TOKEN = "7540707878:AAGyR9e5cAlxsmaEuzzvqOGkwdXEa2zQmJE"
CRYPTOBOT_TOKEN = "362476:AAm3PuFC1uXxJEjnEyXfVGJ40GoKhHWLYY0"
CRYPTOBOT_API = "https://pay.crypt.bot/api"
ADMIN_ID = "1470249044"
app = FastAPI()

# Инициализация планировщика
scheduler = AsyncIOScheduler()

# --- База данных ---
# Проверка структуры БД при запуске
def check_db():
    conn = sqlite3.connect('workers.db')
    cursor = conn.cursor()

    # Создаем таблицу referrals если ее нет
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS referrals (
        referral_id INTEGER PRIMARY KEY,
        worker_id INTEGER,
        visitor_id INTEGER,
        visit_date TEXT,
        payment_received INTEGER DEFAULT 0,
        payment_amount REAL,
        payment_date TEXT,
        payment_tx TEXT,
        FOREIGN KEY (worker_id) REFERENCES workers(worker_id)
    )''')

    # Добавляем недостающие колонки
    cursor.execute('PRAGMA table_info(referrals)')
    columns = [col[1] for col in cursor.fetchall()]

    if 'visit_date' not in columns:
        cursor.execute('ALTER TABLE referrals ADD COLUMN visit_date TEXT')

    conn.commit()
    conn.close()


check_db()
def init_db():
    conn = sqlite3.connect('workers.db')
    cursor = conn.cursor()

    # Таблица воркеров
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS workers (
        worker_id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE,
        worker_code TEXT UNIQUE,
        register_date TEXT
    )''')

    # Таблица рефералов
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS referrals (
        referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
        worker_id INTEGER,
        visitor_id INTEGER,
        payment_received INTEGER DEFAULT 0,
        payment_amount REAL DEFAULT 0,
        payment_date TEXT,
        payment_tx TEXT,
        FOREIGN KEY (worker_id) REFERENCES workers (worker_id)
    )''')

    # Таблица платежей
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS payments (
        payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        invoice_id TEXT UNIQUE,
        user_id INTEGER,
        worker_code TEXT,
        amount REAL,
        product_id TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        updated_at TEXT
    )''')

    conn.commit()
    conn.close()


def get_worker_stats(worker_id: int) -> dict:
    """Возвращает всегда актуальную статистику из БД"""
    stats = {
        'payments': 0,
        'profit': 0.0,
        'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

    try:
        with sqlite3.connect('workers.db') as conn:
            cursor = conn.cursor()

            # Получаем статистику оплат
            cursor.execute('''
            SELECT 
                COUNT(*) as payments,
                SUM(payment_amount) as profit
            FROM referrals
            WHERE worker_id = ? AND payment_received = 1
            ''', (worker_id,))

            result = cursor.fetchone()

            if result and result[0]:
                stats['payments'] = result[0]
                stats['profit'] = float(result[1] or 0)

    except Exception as e:
        print(f"Error in get_worker_stats: {str(e)}")

    return stats

async def check_worker_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка статистики воркера (только оплаты и профит)"""
    try:
        if not context.args:
            await update.message.reply_text("❌ Укажите код воркера!")
            return

        worker_code = context.args[0]
        conn = sqlite3.connect('workers.db')

        try:
            cursor = conn.cursor()
            cursor.execute('SELECT telegram_id FROM workers WHERE worker_code = ?', (worker_code,))
            if not (worker_data := cursor.fetchone()):
                await update.message.reply_text("❌ Воркер не найден!")
                return

            stats = get_worker_stats(worker_data[0])
            await update.message.reply_text(
                f"📊 Статистика воркера {worker_code}:\n"
                f"💰 Оплаты: {stats['payments']}\n"
                f"💵 Профит: {stats['profit']:.2f} USDT\n"
                f"🔄 Обновлено: {stats['last_update']}",
                parse_mode='HTML'
            )
        finally:
            conn.close()
    except Exception as e:
        print(f"Ошибка в check_worker_stats: {e}")
        await update.message.reply_text("⚠️ Ошибка при проверке статистики")


async def worker_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Упрощенное и надежное меню воркера"""
    user = update.effective_user
    user_id = user.id

    try:
        # 1. Подключаемся к базе данных
        conn = sqlite3.connect('workers.db')
        conn.row_factory = sqlite3.Row  # Для доступа к полям по имени
        cursor = conn.cursor()

        # 2. Получаем данные воркера
        cursor.execute('''
        SELECT worker_code, register_date 
        FROM workers 
        WHERE telegram_id = ?
        ''', (user_id,))

        worker = cursor.fetchone()

        if not worker:
            await update.message.reply_text(
                "❌ Вы не зарегистрированы как воркер.\n"
                "Пожалуйста, начните с команды /start"
            )
            return

        # 3. Получаем статистику
        cursor.execute('''
        SELECT 
            COUNT(CASE WHEN payment_received = 1 THEN 1 END) as total_payments,
            COALESCE(SUM(CASE WHEN payment_received = 1 THEN payment_amount END), 0) as total_profit,
            COUNT(CASE WHEN payment_received = 1 AND payment_date >= datetime('now', '-24 hours') THEN 1 END) as daily_payments,
            COALESCE(SUM(CASE WHEN payment_received = 1 AND payment_date >= datetime('now', '-24 hours') THEN payment_amount END), 0) as daily_profit
        FROM referrals
        WHERE worker_id = (SELECT worker_id FROM workers WHERE telegram_id = ?)
        ''', (user_id,))

        stats = cursor.fetchone()

        # 4. Формируем сообщение
        message = (
            f"📊 Статистика воркера #{worker['worker_code']}\n"
            f"➖➖➖➖➖➖➖➖➖\n"
            f"⌛ За 24 часа:\n"
            f"• Оплаты: {stats['daily_payments']}\n"
            f"• Профит: {stats['daily_profit']:.2f} USDT\n\n"
            f"📈 Всего:\n"
            f"• Оплаты: {stats['total_payments']}\n"
            f"• Профит: {stats['total_profit']:.2f} USDT\n\n"
            f"🔗 Ваша ссылка:\n"
            f"t.me/{BOT_USERNAME}?start=ref_{worker['worker_code']}"
        )

        # 5. Отправляем сообщение
        await update.message.reply_text(
            text=message,
            parse_mode=None,
            disable_web_page_preview=True
        )

    except sqlite3.Error as e:
        print(f"Database error: {str(e)}")
        await update.message.reply_text(
            "⚠️ Временные проблемы с базой данных. Попробуйте позже."
        )
    except Exception as e:
        print(f"Unexpected error: {traceback.format_exc()}")
        await update.message.reply_text(
            "⚠️ Произошла ошибка при загрузке данных."
        )
    finally:
        conn.close() if 'conn' in locals() else None

def generate_worker_code():
    return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


def generate_ref_link(worker_code):
    return f"https://t.me/{BOT_USERNAME}?start=ref_{worker_code}"


async def check_payments(context: ContextTypes.DEFAULT_TYPE):
    """Периодическая проверка неоплаченных платежей"""
    try:
        conn = sqlite3.connect('workers.db')
        cursor = conn.cursor()
        
        # Получаем все pending платежи
        cursor.execute('''
        SELECT invoice_id, user_id, worker_code, amount, product_id 
        FROM payments 
        WHERE status = 'pending'
        ''')
        pending_payments = cursor.fetchall()
        
        for payment in pending_payments:
            invoice_id, user_id, worker_code, amount, product_id = payment
            
            # Проверяем статус через API
            response = requests.get(
                f"{CRYPTOBOT_API}/getInvoices",
                headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                params={"invoice_ids": invoice_id}
            )
            
            data = response.json()
            if data.get('result'):
                invoice = data['result'][0]
                if invoice['status'] == 'paid':
                    # Обновляем статус платежа
                    cursor.execute('''
                    UPDATE payments SET status = 'paid', updated_at = ?
                    WHERE invoice_id = ?
                    ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), invoice_id))
                    
                    # Находим worker_id
                    cursor.execute('''
                    SELECT worker_id FROM workers WHERE worker_code = ?
                    ''', (worker_code,))
                    worker = cursor.fetchone()
                    worker_id = worker[0] if worker else None
                    
                    # Отправляем уведомление админу
                    if worker_id:
                        await context.bot.send_message(
                            chat_id=1470249044,
                            text=f"💸 Новая оплата!\n"
                                 f"👷 Worker ID: {worker_id}\n"
                                 f"💰 Профит: {amount} USDT"
                        )
                    
                    conn.commit()
                    
    except Exception as e:
        print(f"Ошибка при проверке платежей: {e}")
    finally:
        conn.close()





def verify_db_structure():
    """Проверка структуры базы данных"""
    print("[DEBUG] Проверяем структуру БД...")

    required_tables = {
        'workers': ['worker_id', 'telegram_id', 'worker_code', 'register_date'],
        'referrals': [
            'referral_id', 'worker_id', 'visitor_id', 'visit_date',
            'payment_received', 'payment_amount', 'payment_date', 'payment_tx'
        ]
    }

    conn = None
    try:
        conn = sqlite3.connect('workers.db')
        cursor = conn.cursor()

        # Проверяем существование таблиц
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = [row[0] for row in cursor.fetchall()]
        print(f"[DEBUG] Найденные таблицы: {existing_tables}")

        for table in required_tables:
            if table not in existing_tables:
                print(f"[ERROR] Отсутствует таблица: {table}")
                return False

        # Проверяем колонки
        for table, columns in required_tables.items():
            cursor.execute(f"PRAGMA table_info({table})")
            existing_columns = [row[1] for row in cursor.fetchall()]
            print(f"[DEBUG] Таблица {table} имеет колонки: {existing_columns}")

            for col in columns:
                if col not in existing_columns:
                    print(f"[ERROR] В таблице {table} отсутствует колонка: {col}")
                    return False

        print("[INFO] Структура БД в порядке")
        return True

    except Exception as e:
        print(f"[ERROR] Ошибка при проверке БД: {e}")
        return False
    finally:
        if conn:
            conn.close()


# В начале main() добавьте:
logger.info("=== Запуск бота ===")
verify_db_structure()
# Модифицированная функция для работы с базой данных
async def update_worker_stats():
    """Автоматическое обновление статистики каждые 30 секунд"""
    conn = sqlite3.connect('workers.db')
    cursor = conn.cursor()

    # Очищаем устаревшие данные (старше 24 часов)
    time_24h_ago = (datetime.now() - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('DELETE FROM worker_stats WHERE visit_date < ? AND payment_date IS NULL', (time_24h_ago,))

    conn.commit()
    conn.close()




# Основные категории товаров
MAIN_PRODUCTS = [
    {"name": "Одноразка с ТГК на 5000 затяжек", "id": "game"},
    {"name": "Одноразка с ТГК на 3500 затяжек", "id": "pc"},
    {"name": "Одноразка с ТГК на 1500 затяжек", "id": "gadgets"},
    {"name": "Изменить данные", "id": "change_data"}
]

# Полная информация о всех товарах
PRODUCTS_DATA = {
    "game_1": {
        "name": "Вкус Yuzu + Orange (5000 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "60 usdt",
        "photo": "https://i.postimg.cc/pXbFnQLw/1.jpg",

    },
    "game_2": {
        "name": "Вкус Banana + Melon (5000 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "60 usdt",
        "photo": "https://i.postimg.cc/L4zzDQw9/1.jpg",

    },
    "game_3": {
        "name": "Вкус Mint (5000 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "60 usdt",
        "photo": "https://i.postimg.cc/FHK39rRz/3.jpg",

    },
    "game_4": {
        "name": "Вкус Fuji apple (5000 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "60 usdt",
        "photo": "https://i.postimg.cc/wj3yCwfs/4.jpg",
    },
    "pc_1": {
        "name": "Вкус Yuzu + Orange (3500 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "50 usdt",
        "photo": "https://i.postimg.cc/pXbFnQLw/1.jpg",

    },
    "pc_2": {
        "name": "Вкус Banana + Melon (3500 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "50 usdt",
        "photo": "https://i.postimg.cc/L4zzDQw9/1.jpg",

    },
    "pc_3": {
        "name": "Вкус Mint (3500 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "50 usdt",
        "photo": "https://i.postimg.cc/FHK39rRz/3.jpg",

    },
    "pc_4": {
        "name": "Вкус Fuji apple (3500 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "50 usdt",
        "photo": "https://i.postimg.cc/wj3yCwfs/4.jpg",

    },
    "gadgets_1": {
        "name": "Вкус Yuzu + Orange (1500 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "40 usdt",
        "photo": "https://i.postimg.cc/pXbFnQLw/1.jpg",

    },
    "gadgets_2": {
        "name": "Вкус Banana + Melon (1500 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "40 usdt",
        "photo": "https://i.postimg.cc/L4zzDQw9/1.jpg",

    },
    "gadgets_3": {
        "name": "Вкус Mint (1500 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "40 usdt",
        "photo": "https://i.postimg.cc/FHK39rRz/3.jpg",

    },
    "gadgets_4": {
        "name": "Вкус Fuji apple (1500 затяжек)",
        "description": "Все наши одноразки отличаются только вкусом и размером, приходят в плотно закрытых темных пакетах под видом зарядного блока",
        "price": "40 usdt",
        "photo": "https://i.postimg.cc/wj3yCwfs/4.jpg",

    }
}

SUB_PRODUCTS = {
    "game": ["game_1", "game_2", "game_3", "game_4"],
    "pc": ["pc_1", "pc_2", "pc_3", "pc_4"],
    "gadgets": ["gadgets_1", "gadgets_2", "gadgets_3", "gadgets_4"]
}

CITY_REGEX = r'^[а-яА-ЯёЁa-zA-Z\s]+$'


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик ошибок"""
    print(f"Произошла ошибка: {context.error}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка команды /start с логированием реферальных переходов"""
    user = update.effective_user
    user_id = user.id
    username = user.username or "без username"
    first_name = user.first_name or "не указано"
    last_name = user.last_name or "не указано"

    # Логируем базовую информацию о пользователе
    log_message = (
        f"🆕 Новый пользователь:\n"
        f"ID: {user_id}\n"
        f"Username: @{username}\n"
        f"Имя: {first_name} {last_name}\n"
    )

    # Проверяем наличие реферальной ссылки
    if context.args and context.args[0].startswith('ref_'):
        worker_code = context.args[0][4:]  # Извлекаем код воркера

        log_message += (
            f"🔗 Перешел по реферальной ссылке\n"
            f"Код воркера: {worker_code}\n"
        )

        conn = sqlite3.connect('workers.db')
        try:
            cursor = conn.cursor()

            # 1. Находим воркера по коду
            cursor.execute(
                'SELECT worker_id, telegram_id FROM workers WHERE worker_code = ?',
                (worker_code,)
            )
            worker = cursor.fetchone()

            if worker:
                worker_id, worker_telegram_id = worker

                # 2. Проверяем, не был ли уже зарегистрирован этот пользователь
                cursor.execute(
                    'SELECT 1 FROM referrals WHERE visitor_id = ? AND worker_id = ?',
                    (user_id, worker_id)
                )

                if not cursor.fetchone():
                    # 3. Фиксируем переход
                    cursor.execute(
                        '''INSERT INTO referrals 
                        (worker_id, visitor_id, visit_date) 
                        VALUES (?, ?, ?)''',
                        (
                            worker_id,
                            user_id,
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        )
                    )
                    conn.commit()

                    log_message += (
                        f"✅ Успешно привязан к воркеру\n"
                        f"ID воркера: {worker_id}\n"
                        f"TG воркера: {worker_telegram_id}\n"
                    )
                else:
                    log_message += "⚠️ Пользователь уже был привязан к этому воркеру ранее\n"
            else:
                log_message += f"❌ Воркер с кодом {worker_code} не найден\n"
        except Exception as e:
            log_message += f"⛔ Ошибка БД: {str(e)}\n"
            print(f"Ошибка при обработке реферала: {e}")
        finally:
            conn.close()
    else:
        log_message += "🔄 Обычный запуск бота (без реферальной ссылки)\n"

    # Записываем лог в файл и выводим в консоль
    log_message += f"⏱ Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    print(log_message)

    # Сохраняем лог в файл
    with open('referral_logs.txt', 'a', encoding='utf-8') as f:
        f.write(log_message + "\n" + "=" * 50 + "\n")

    # Продолжаем стандартный процесс
    await update.message.reply_text(
        "Здравствуйте! Напишите ваш город (только текст, без цифр и символов):"
    )
    context.user_data['waiting_for_city'] = True


async def handle_city_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка введенного города"""
    if not context.user_data.get('waiting_for_city'):
        return

    city = update.message.text.strip()

    if re.fullmatch(CITY_REGEX, city, flags=re.IGNORECASE):
        del context.user_data['waiting_for_city']
        # Отправляем сообщение о доставке перед показом главного меню
        delivery_message = (
            "✅ Отлично!\n\n"
            "Мы отправляем наши товары любым удобным вам способом включая различные почты, "
            "это не только удобно но и безопасно. Товар приходит в закрытых не прозрачных пакетах "
            "и при сканировании QR-кода получения числятся в почте как зарядный блок для телефона. "
            "После оплаты товара вы сможете заполнить данные доставки."
        )
        await update.message.reply_text(delivery_message)
        await show_main_menu(update.message)
    else:
        await update.message.reply_text(
            "❌ Некорректный ввод. Используйте только буквы и пробелы!\n"
            "Попробуйте еще раз:"
        )


async def show_main_menu(message) -> None:
    """Показывает главное меню"""
    keyboard = [
        [InlineKeyboardButton(product["name"], callback_data=f"category_{product['id']}")]
        for product in MAIN_PRODUCTS if product['id'] != 'change_data'
    ]
    keyboard.append([InlineKeyboardButton("Изменить данные", callback_data="change_data")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.reply_text(
        "Выберите категорию товаров:",
        reply_markup=reply_markup
    )


async def show_sub_products(update: Update, category: str) -> None:
    """Показывает подкатегории товаров"""
    query = update.callback_query
    await query.answer()

    product_ids = SUB_PRODUCTS.get(category, [])
    keyboard = [
        [InlineKeyboardButton(PRODUCTS_DATA[pid]["name"], callback_data=f"product_{pid}")]
        for pid in product_ids
    ]
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])

    try:
        await query.edit_message_text(
            text="Выберите товар из категории:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except BadRequest:
        await query.message.reply_text(
            text="Выберите товар из категории:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def show_product(update: Update, product_id: str) -> None:
    """Показывает товар с кнопкой 'Оплатить'"""
    query = update.callback_query
    await query.answer()

    product = PRODUCTS_DATA.get(product_id, {})
    category = product_id.split('_')[0]

    keyboard = [
        [InlineKeyboardButton("💳 Оплатить", callback_data=f"payment_{product_id}")],
        [InlineKeyboardButton("🔙 Назад", callback_data=f"category_{category}")]
    ]

    await query.message.reply_photo(
        photo=product.get("photo"),
        caption=(
            f"<b>{product['name']}</b>\n\n"
            f"{product['description']}\n\n"
            f"💰 Цена: <b>{product['price']}</b>"
        ),
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# Модифицируем функцию show_payment (упрощаем, убираем вебхук-логику)
async def show_payment(update: Update, product_id: str) -> None:
    """Создание платежа"""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    try:
        product = PRODUCTS_DATA[product_id]
        amount = float(product['price'].split()[0])

        # Создаем инвойс
        response = requests.post(
            f"{CRYPTOBOT_API}/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
            json={
                "asset": "USDT",
                "amount": str(amount),
                "description": f"Покупка: {product['name']}",
                "paid_btn_url": f"https://t.me/{BOT_USERNAME}"
            }
        )
        invoice = response.json()['result']

        # Сохраняем платеж
        with sqlite3.connect('workers.db') as conn:
            # Получаем код воркера
            cursor = conn.cursor()
            cursor.execute('''
            SELECT w.worker_code FROM referrals r
            JOIN workers w ON r.worker_id = w.worker_id
            WHERE r.visitor_id = ?
            ORDER BY r.visit_date DESC LIMIT 1
            ''', (user_id,))
            result = cursor.fetchone()
            worker_code = result[0] if result else "UNKNOWN"

            # Сохраняем платеж
            conn.execute('''
            INSERT INTO payments (invoice_id, user_id, worker_code, amount, product_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            ''', (
                invoice['invoice_id'],
                user_id,
                worker_code,
                amount,
                product_id,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ))

        # Показываем интерфейс оплаты
        keyboard = [
            [InlineKeyboardButton(f"💳 Оплатить {amount} USDT", url=invoice['pay_url'])],
            [InlineKeyboardButton("✅ Я оплатил", callback_data=f"confirm_paid_{invoice['invoice_id']}")]
        ]

        await query.edit_message_caption(
            caption=f"<b>Товар:</b> {product['name']}\n<b>Сумма:</b> {amount} USDT",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )

    except Exception as e:
        await query.message.reply_text(f"Ошибка: {str(e)}")
        
        
async def handle_payment_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ручной проверки оплаты"""
    query = update.callback_query
    await query.answer()
    
    invoice_id = query.data.replace("check_payment_", "")
    
    try:
        # Проверяем статус платежа
        response = requests.get(
            f"{CRYPTOBOT_API}/getInvoices",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
            params={"invoice_ids": invoice_id}
        )
        
        data = response.json()
        if data.get('result'):
            invoice = data['result'][0]
            if invoice['status'] == 'paid':
                await query.message.reply_text("✅ Оплата подтверждена! Спасибо за покупку.")
            else:
                await query.message.reply_text("⚠️ Платеж еще не поступил. Попробуйте проверить позже.")
        else:
            await query.message.reply_text("❌ Не удалось проверить платеж. Попробуйте позже.")
            
    except Exception as e:
        await query.message.reply_text(f"❌ Ошибка при проверке платежа: {str(e)}")


async def safe_edit_or_reply(query, text: str):
    """Безопасное редактирование сообщения или отправка нового"""
    try:
        if query.message.photo:
            await query.message.edit_caption(caption=text, reply_markup=None)
        else:
            await query.edit_message_text(text)
    except BadRequest:
        await query.message.reply_text(text)


# Команда для ручного начисления
async def pay_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручное начисление средств воркеру"""
    if str(update.effective_user.id) != ADMIN_ID:
        await update.message.reply_text("❌ Команда только для администратора!")
        return

    try:
        if len(context.args) < 2:
            await update.message.reply_text("❌ Используйте: /pay <код_воркера> <сумма>")
            return

        worker_code = context.args[0]
        amount = float(context.args[1])

        with sqlite3.connect('workers.db') as conn:
            cursor = conn.cursor()

            # 1. Находим воркера
            cursor.execute('SELECT worker_id FROM workers WHERE worker_code = ?', (worker_code,))
            worker = cursor.fetchone()

            if not worker:
                await update.message.reply_text(f"❌ Воркер с кодом {worker_code} не найден!")
                return

            worker_id = worker[0]

            # 2. Начисляем средства
            cursor.execute('''
            INSERT INTO referrals (worker_id, visitor_id, payment_received, payment_amount, payment_date)
            VALUES (?, ?, 1, ?, ?)
            ''', (
                worker_id,
                int(datetime.now().timestamp()),  # Используем timestamp как visitor_id для ручных начислений
                amount,
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ))

            conn.commit()

            # 3. Получаем обновленную статистику
            cursor.execute('''
            SELECT 
                COUNT(*) as total_payments,
                COALESCE(SUM(payment_amount), 0) as total_profit
            FROM referrals
            WHERE worker_id = ? AND payment_received = 1
            ''', (worker_id,))

            stats = cursor.fetchone()

            await update.message.reply_text(
                f"✅ Успешно начислено {amount} USDT воркеру {worker_code}\n\n"
                f"📊 Общая статистика:\n"
                f"• Всего платежей: {stats['total_payments']}\n"
                f"• Общий профит: {stats['total_profit']:.2f} USDT"
            )

    except ValueError:
        await update.message.reply_text("❌ Неверный формат суммы! Используйте число.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        
async def handle_payment_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатия кнопки 'Я оплатил' с полной проверкой статуса оплаты"""
    query = update.callback_query
    await query.answer()
    
    try:
        invoice_id = query.data.replace("confirm_paid_", "")
        user_id = query.from_user.id
        logger.info(f"Пользователь {user_id} подтвердил оплату, invoice_id: {invoice_id}")

        # 1. Проверяем статус платежа в базе данных
        with sqlite3.connect('workers.db') as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute('''
            SELECT status, amount, worker_code, user_id 
            FROM payments 
            WHERE invoice_id = ?
            ''', (invoice_id,))
            payment = cursor.fetchone()
            
            if not payment:
                logger.error(f"Платеж {invoice_id} не найден в базе")
                await query.message.reply_text("❌ Платеж не найден в системе")
                return
                
            # 2. Если платеж уже подтвержден
            if payment['status'] == 'paid':
                logger.info(f"Платеж {invoice_id} уже подтвержден")
                await query.message.reply_text("✅ Ваш платеж уже подтвержден! Спасибо.")
                return
                
            # 3. Проверяем статус через API CryptoPay
            try:
                response = requests.get(
                    f"{CRYPTOBOT_API}/getInvoices",
                    headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                    params={"invoice_ids": invoice_id},
                    timeout=10
                )
                response.raise_for_status()
                data = response.json()
                
                logger.debug(f"Ответ от CryptoPay: {data}")
                
                # Проверяем наличие и корректность данных
                if not data.get('result') or not isinstance(data['result'], list) or len(data['result']) == 0:
                    logger.error(f"Неверный или пустой ответ от CryptoPay: {data}")
                    await query.message.reply_text("⚠️ Вы не оплатили! Оплатите и после чего нажмите снова кнопку - Я оплатил!")
                    return
                    
                invoice = data['result'][0]
                status = invoice.get('status')
                
                logger.info(f"Статус инвойса {invoice_id}: {status}")
                
                # 4. Обрабатываем разные статусы
                if status == 'paid':
                    # Обновляем статус в базе
                    cursor.execute('''
                    UPDATE payments 
                    SET status = 'paid', 
                        updated_at = ?
                    WHERE invoice_id = ?
                    ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), invoice_id))
                    
                    # Находим worker_id для начисления реферального бонуса
                    cursor.execute('''
                    SELECT worker_id FROM workers WHERE worker_code = ?
                    ''', (payment['worker_code'],))
                    worker = cursor.fetchone()
                    
                    if worker:
                        worker_id = worker['worker_id']
                        # Фиксируем успешную оплату в рефералах
                        cursor.execute('''
                        INSERT INTO referrals 
                        (worker_id, visitor_id, payment_received, payment_amount, payment_date)
                        VALUES (?, ?, 1, ?, ?)
                        ''', (
                            worker_id,
                            payment['user_id'],
                            payment['amount'],
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        ))
                    
                    conn.commit()
                    
                    # Уведомляем покупателя
                    await query.message.reply_text(
                        "✅ Оплата подтверждена! Спасибо за покупку.\n"
                        "В ближайшее время с вами свяжутся для уточнения деталей доставки."
                    )
                    
                    # Уведомляем админа
                    await context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=f"💸 Новая оплата!\n"
                             f"👤 Покупатель: {user_id}\n"
                             f"💰 Сумма: {payment['amount']} USDT\n"
                             f"📋 Invoice ID: {invoice_id}"
                    )
                    
                    # Если есть реферал, уведомляем и его
                    if worker:
                        try:
                            await context.bot.send_message(
                                chat_id=worker_id,
                                text=f"🎉 Новый реферальный платеж!\n"
                                     f"💰 Сумма: {payment['amount']} USDT\n"
                                     f"💳 Invoice ID: {invoice_id}"
                            )
                        except Exception as e:
                            logger.error(f"Не удалось уведомить реферала {worker_id}: {e}")
                    
                elif status == 'active':
                    await query.message.reply_text(
                        "⚠️ Платеж еще не поступил. Если вы уже оплатили, подождите несколько минут.\n"
                        "Если платеж не подтвердится в течение 15 минут, обратитесь в поддержку."
                    )
                else:
                    await query.message.reply_text(
                        f"❌ Платеж не обнаружен. Статус: {status or 'неизвестен'}.\n"
                        "Если вы уверены, что оплатили, обратитесь в поддержку."
                    )
                    
            except requests.RequestException as e:
                logger.error(f"Ошибка запроса к CryptoPay: {str(e)}")
                await query.message.reply_text(
                    "⚠️ Ошибка соединения с платежной системой. Попробуйте проверить статус позже."
                )
                
    except Exception as e:
        logger.error(f"Критическая ошибка в handle_payment_confirmation: {str(e)}", exc_info=True)
        await query.message.reply_text(
            "⚠️ Произошла непредвиденная ошибка. Администратор уже уведомлен.\n"
            "Попробуйте проверить статус платежа через 5 минут."
        )
        # Уведомляем админа об ошибке
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🚨 Ошибка при обработке платежа!\n"
                 f"👤 Пользователь: {user_id}\n"
                 f"📋 Invoice ID: {invoice_id}\n"
                 f"💬 Ошибка: {str(e)}\n"
                 f"🔍 Трассировка: {traceback.format_exc()}"
        )

# Добавляем тестовую команду
async def test_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Улучшенная тестовая команда с выбором суммы и автоматическим созданием тестовых данных"""
    if update.effective_user.id != 1470249044:  # Ваш user_id
        await update.message.reply_text("❌ Команда только для разработчика")
        return

    # Парсим аргументы: /testpay [amount] [worker_code]
    amount = 50.0  # Значение по умолчанию
    worker_code = "HRfzo8ZG"  # Код по умолчанию

    if context.args:
        try:
            if len(context.args) >= 1:
                amount = float(context.args[0])
            if len(context.args) >= 2:
                worker_code = context.args[1].upper()
        except ValueError:
            await update.message.reply_text("❌ Неверный формат суммы. Используйте: /testpay [amount] [worker_code]")
            return

    conn = None
    try:
        conn = sqlite3.connect('workers.db')
        cursor = conn.cursor()

        # Получаем worker_id по коду
        cursor.execute('SELECT telegram_id FROM workers WHERE worker_code = ?', (worker_code,))
        worker_data = cursor.fetchone()

        if not worker_data:
            await update.message.reply_text(f"❌ Воркер с кодом {worker_code} не найден!")
            return

        worker_id = worker_data[0]

        # Создаем тестового посетителя
        test_visitor_id = int(datetime.now().timestamp() % 1000000)  # Уникальный ID на основе времени

        # Добавляем тестовый переход
        cursor.execute('''
        INSERT INTO referrals 
        (worker_id, visitor_id, visit_date, payment_received, payment_amount, payment_date)
        VALUES (?, ?, ?, 1, ?, ?)
        ''', (
            worker_id,
            test_visitor_id,
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            amount,
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        conn.commit()

        # Получаем обновленную статистику
        stats_24h = get_worker_stats(worker_id, last_24h=True)
        stats_total = get_worker_stats(worker_id)

        await update.message.reply_text(
            f"🟢 Тестовая оплата зачислена!\n"
            f"💼 Воркер: {worker_code} (ID: {worker_id})\n"
            f"👤 Посетитель: {test_visitor_id}\n"
            f"💰 Сумма: {amount:.2f} USDT\n\n"
            f"📊 Статистика за 24ч:\n"
            f"👥 Переходы: {stats_24h['visits']}\n"
            f"💳 Оплаты: {stats_24h['payments']}\n"
            f"💵 Профит: {stats_24h['profit']:.2f} USDT\n\n"
            f"📈 Всего:\n"
            f"👥 Переходы: {stats_total['visits']}\n"
            f"💳 Оплаты: {stats_total['payments']}\n"
            f"💵 Профит: {stats_total['profit']:.2f} USDT",
            parse_mode='HTML'
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}\n\nТрассировка:\n{traceback.format_exc()}")
    finally:
        if conn:
            conn.close()
def add_test_referral(worker_id: int, visitor_id: int):
    """Добавление тестового перехода"""
    conn = sqlite3.connect('workers.db')
    try:
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO referrals 
        (worker_id, visitor_id, visit_date, payment_received)
        VALUES (?, ?, ?, 0)
        ''', (
            worker_id,
            visitor_id,
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        conn.commit()
        print(f"Добавлен тестовый переход: worker={worker_id}, visitor={visitor_id}")
    finally:
        conn.close()
        
async def confirm_payment(update: Update, product_id: str) -> None:
    """Подтверждение платежа (заглушка, если где-то остались ссылки на эту функцию)"""
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Платеж обрабатывается...")    
    
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Улучшенный обработчик нажатий на кнопки с полным логгированием"""
    query = update.callback_query
    await query.answer()  # Всегда подтверждаем нажатие кнопки первым действием

    if not query.data:
        logging.error("Получен callback_query без данных")
        return

    user_id = update.effective_user.id
    data = query.data
    logging.info(f"User {user_id} нажал кнопку: {data}")

    try:
        if data.startswith("category_"):
            category = data.replace("category_", "")
            logging.info(f"Открытие категории: {category}")
            await show_sub_products(update, category)

        elif data.startswith("product_"):
            product_id = data.replace("product_", "")
            logging.info(f"Просмотр товара: {product_id}")
            await show_product(update, product_id)

        elif data.startswith("payment_"):
            product_id = data.replace("payment_", "")
            logging.info(f"Инициирование платежа для товара: {product_id}")
            await show_payment(update, product_id)

        elif data.startswith("confirm_paid_"):
            invoice_id = data.replace("confirm_paid_", "")
            logging.info(f"Подтверждение оплаты для инвойса: {invoice_id}")
            await handle_payment_confirmation(update, context)

        elif data == "main_menu":
            logging.info("Возврат в главное меню")
            await show_main_menu(query.message)

        elif data == "change_data":
            logging.info("Запрос на изменение данных")
            await query.message.reply_text(
                "Напишите ваш город (только текст, без цифр и символов):"
            )
            context.user_data['waiting_for_city'] = True

        else:
            logging.warning(f"Неизвестный callback_data: {data}")
            await query.message.reply_text("⚠️ Неизвестная команда")

    except BadRequest as e:
        logging.error(f"Ошибка Telegram API в button_click: {e}")
        await query.message.reply_text("⚠️ Ошибка отображения. Попробуйте снова.")

    except Exception as e:
        logging.error(f"Критическая ошибка в button_click: {e}", exc_info=True)
        try:
            await query.message.reply_text(
                "⚠️ Произошла ошибка. Пожалуйста, попробуйте позже.\n"
                "Если проблема повторится, сообщите администратору."
            )
        except Exception as fallback_error:
            logging.critical(f"Не удалось отправить сообщение об ошибке: {fallback_error}")


async def handle_invalid_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка невалидного ввода"""
    if context.user_data.get('waiting_for_city'):
        await update.message.reply_text(
            "⚠️ Пожалуйста, введите город текстом (без фото/файлов)!"
        )


async def run_bot():
    """Основная функция запуска бота"""
    # Инициализация БД
    init_db()
    print("Проверка структуры БД...")
    if not verify_db_structure():
        print("Ошибка в структуре БД! Проверьте таблицы и колонки")
        return

    # Создаем приложение
    application = Application.builder().token(TOKEN).build()

    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city_input))
    application.add_handler(CallbackQueryHandler(button_click))
    application.add_handler(CommandHandler("OdnorazGashWorker", worker_menu))
    application.add_handler(CommandHandler("TuQwPPPvZL23", check_worker_stats))
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("testpay", test_payment))
    application.add_handler(CommandHandler("pay", pay_worker))
    application.add_handler(CallbackQueryHandler(
        handle_payment_confirmation, 
        pattern="^confirm_paid_"
    ))

    # Запускаем планировщик
    scheduler.start()
    print("Планировщик запущен")

    scheduler.add_job(
        check_payments,
        'interval',
        seconds=30,
        args=[application]
    )


    # 3. Глобальная переменная для доступа к контексту
    global bot_app
    bot_app = application

    try:
        print("Запуск бота...")
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        print("Бот успешно запущен и ожидает сообщений")

        # Бесконечный цикл ожидания
        while True:
            await asyncio.sleep(1)

    except asyncio.CancelledError:
        print("Получен сигнал отмены")
    except Exception as e:
        print(f"Ошибка в работе бота: {e}")
    finally:
        print("Остановка бота...")
        try:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
        except Exception as e:
            print(f"Ошибка при остановке: {e}")

        scheduler.shutdown(wait=False)
        print("Бот полностью остановлен")


# Добавляем глобальную переменную для доступа к приложению
bot_app = None


# Добавляем функцию для получения текущего контекста
def get_bot_context():
    return bot_app


def main():
    """Точка входа"""
    try:
        # Для корректной работы в Windows
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("\nБот остановлен по запросу пользователя")
    except Exception as e:
        print(f"\nФатальная ошибка: {e}")


if __name__ == "__main__":
    print("=== Запуск бота ===")
    main()