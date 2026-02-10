import asyncio

async def main():
    # Ваш существующий код, который был после:
    # if __name__ == '__main__':
    # Замените это на асинхронную функцию
    
    # Например, если у вас было:
    # try:
    #     asyncio.run(main())
    # except KeyboardInterrupt:
    #     print("\nСкрипт зупинено вручну")
    
    # Теперь делаем так:
    print(">>> Підключення до Telegram...")
    
    await app.start()
    print(">>> УСПІХ! Скрипт запущено")
    
    try:
        while True:
            for chat in CHATS:
                try:
                    await app.send_message(chat, MESSAGE)
                    print(f"[{time.strftime('%H:%M:%S')}] отправлено в {chat}")
                    await asyncio.sleep(random.randint(5, 10))
                except Exception as e:
                    print(f"Помилка в {chat}: {e}")
            
            delay = 60 + random.randint(10, 20)
            print(f"Очікування {delay} сек...")
            await asyncio.sleep(delay)
    
    except asyncio.CancelledError:
        # Это нормально - нас остановили
        pass
    finally:
        await app.stop()import os

import asyncio
import logging
import sqlite3
import time
import random
from datetime import datetime
from typing import Dict, List, Optional

from pyrogram import Client
from pyrogram.errors import FloodWait
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

# ========== НАСТРОЙКИ ==========
# Берем токен из переменных окружения Render
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Путь к базе данных (на Render используем абсолютный путь)
if os.getenv("RENDER"):
    # На Render файловая система эфемерная, лучше использовать /tmp
    DATABASE_NAME = "/tmp/spam_bot.db"
else:
    DATABASE_NAME = "spam_bot.db"

# Настройка логирования для Render
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== БАЗА ДАННЫХ (оптимизированная для Render) ==========
class Database:
    def __init__(self):
        # Создаем папку для базы данных если её нет
        if os.path.dirname(DATABASE_NAME):
            os.makedirs(os.path.dirname(DATABASE_NAME), exist_ok=True)
        
        self.conn = sqlite3.connect(DATABASE_NAME, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")  # Для лучшей производительности
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        
        # Таблица пользователей бота
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица Telegram аккаунтов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS telegram_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_user_id INTEGER,
                session_name TEXT,
                api_id INTEGER,
                api_hash TEXT,
                phone_number TEXT,
                first_name TEXT,
                username TEXT,
                is_active BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (bot_user_id) REFERENCES bot_users (user_id)
            )
        ''')
        
        # Таблица чатов для рассылки
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS spam_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                chat_username TEXT,
                custom_text TEXT,
                is_active BOOLEAN DEFAULT 1,
                UNIQUE(account_id, chat_username),
                FOREIGN KEY (account_id) REFERENCES telegram_accounts (id)
            )
        ''')
        
        # Таблица текстов для рассылки
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS spam_texts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER PRIMARY KEY,
                general_text TEXT,
                FOREIGN KEY (account_id) REFERENCES telegram_accounts (id)
            )
        ''')
        
        # Таблица активных рассылок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_spam_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                status TEXT DEFAULT 'running',
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES telegram_accounts (id)
            )
        ''')
        
        self.conn.commit()
        logger.info("База данных инициализирована")

# ========== ОСТАЛЬНОЙ КОД БЕЗ ИЗМЕНЕНИЙ ==========
# (Вставьте сюда весь остальной код из предыдущего сообщения, начиная с класса Database)
# Классы Database, AccountManager, Form и все обработчики остаются без изменений

# ========== ОСНОВНОЙ ЗАПУСК ==========
async def on_startup(dp):
    logger.info("Бот запущен на Render!")
    logger.info(f"Используется база данных: {DATABASE_NAME}")
    
    # Проверяем токен бота
    if API_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ Токен бота не установлен! Установите переменную окружения TELEGRAM_BOT_TOKEN")
        logger.info("На Render: Settings -> Environment -> Add Environment Variable")
        logger.info("Ключ: TELEGRAM_BOT_TOKEN")
        logger.info("Значение: ваш_токен_бота")
    
    # Инициализируем базу данных
    global db, account_manager
    db = Database()
    account_manager = AccountManager()

async def on_shutdown(dp):
    logger.info("Бот выключается...")
    # Останавливаем все активные рассылки
    for account_id in list(account_manager.spam_tasks.keys()):
        await account_manager.stop_spam(account_id)
    logger.info("Все рассылки остановлены")

if __name__ == '__main__':
    # Проверяем, запущены ли мы на Render
    if os.getenv("RENDER"):
        logger.info("Обнаружен Render. Используем настройки для облака.")
    
    # Создаем бота и диспетчер
    bot = Bot(token=API_TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(bot, storage=storage)
    
    # Инициализируем базу и менеджер аккаунтов
    db = Database()
    account_manager = AccountManager()
    
    # Регистрируем обработчики команд
    @dp.message_handler(commands=['start'])
    async def cmd_start(message: types.Message):
        user_id = message.from_user.id
        db.add_bot_user(user_id)
        
        await message.answer(
            "👋 Добро пожаловать в бота для управления Telegram-рассылкой!\n"
            "Выберите действие:",
            reply_markup=get_main_keyboard(user_id)
        )
    
    # ... (вставьте все остальные обработчики из предыдущего кода)
    
    # Запускаем бота
    executor.start_polling(
        dp, 
        skip_updates=True, 
        on_startup=on_startup,
        on_shutdown=on_shutdown
    )