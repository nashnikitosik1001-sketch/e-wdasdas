import os
import asyncio
import logging
import sqlite3
import time
import random
from datetime import datetime
from typing import Dict, List, Optional
from threading import Thread

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
    DATABASE_NAME = "/tmp/spam_bot.db"
else:
    DATABASE_NAME = "spam_bot.db"

# Настройка логирования для Render
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== БАЗА ДАННЫХ ==========
class Database:
    def __init__(self):
        if os.path.dirname(DATABASE_NAME):
            os.makedirs(os.path.dirname(DATABASE_NAME), exist_ok=True)
        
        self.conn = sqlite3.connect(DATABASE_NAME, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
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
    
    # Методы для работы с пользователями
    def add_bot_user(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO bot_users (user_id) VALUES (?)', (user_id,))
        self.conn.commit()
    
    # Методы для работы с аккаунтами
    def add_telegram_account(self, bot_user_id: int, session_name: str, 
                           api_id: int, api_hash: str, phone_number: str = None):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO telegram_accounts 
            (bot_user_id, session_name, api_id, api_hash, phone_number)
            VALUES (?, ?, ?, ?, ?)
        ''', (bot_user_id, session_name, api_id, api_hash, phone_number))
        self.conn.commit()
        return cursor.lastrowid
    
    def update_account_info(self, account_id: int, first_name: str = None, 
                          username: str = None, is_active: bool = None):
        cursor = self.conn.cursor()
        if first_name and username:
            cursor.execute('''
                UPDATE telegram_accounts 
                SET first_name = ?, username = ?, is_active = ?
                WHERE id = ?
            ''', (first_name, username, is_active or 0, account_id))
        elif is_active is not None:
            cursor.execute('UPDATE telegram_accounts SET is_active = ? WHERE id = ?', 
                          (is_active, account_id))
        self.conn.commit()
    
    def get_user_accounts(self, bot_user_id: int):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT id, session_name, first_name, username, is_active
            FROM telegram_accounts 
            WHERE bot_user_id = ?
            ORDER BY created_at DESC
        ''', (bot_user_id,))
        return cursor.fetchall()
    
    def get_account_info(self, account_id: int):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM telegram_accounts WHERE id = ?', (account_id,))
        return cursor.fetchone()
    
    def delete_account(self, account_id: int):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM telegram_accounts WHERE id = ?', (account_id,))
        cursor.execute('DELETE FROM spam_chats WHERE account_id = ?', (account_id,))
        cursor.execute('DELETE FROM spam_texts WHERE account_id = ?', (account_id,))
        cursor.execute('DELETE FROM active_spam_sessions WHERE account_id = ?', (account_id,))
        self.conn.commit()
    
    # Методы для работы с чатами
    def add_chats(self, account_id: int, chat_usernames: List[str]):
        cursor = self.conn.cursor()
        for chat in chat_usernames:
            cursor.execute('''
                INSERT OR IGNORE INTO spam_chats (account_id, chat_username)
                VALUES (?, ?)
            ''', (account_id, chat.strip()))
        self.conn.commit()
    
    def get_account_chats(self, account_id: int):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT chat_username, custom_text, is_active
            FROM spam_chats 
            WHERE account_id = ?
            ORDER BY id
        ''', (account_id,))
        return cursor.fetchall()
    
    def update_chat_text(self, account_id: int, chat_username: str, custom_text: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE spam_chats 
            SET custom_text = ?
            WHERE account_id = ? AND chat_username = ?
        ''', (custom_text, account_id, chat_username))
        self.conn.commit()
    
    def delete_chat(self, account_id: int, chat_username: str):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM spam_chats WHERE account_id = ? AND chat_username = ?', 
                      (account_id, chat_username))
        self.conn.commit()
    
    # Методы для работы с текстами
    def set_general_text(self, account_id: int, text: str):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO spam_texts (account_id, general_text)
            VALUES (?, ?)
        ''', (account_id, text))
        self.conn.commit()
    
    def get_general_text(self, account_id: int):
        cursor = self.conn.cursor()
        cursor.execute('SELECT general_text FROM spam_texts WHERE account_id = ?', (account_id,))
        result = cursor.fetchone()
        return result[0] if result else None
    
    # Методы для работы с активными рассылками
    def start_spam_session(self, account_id: int):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO active_spam_sessions (account_id, status)
            VALUES (?, 'running')
        ''', (account_id,))
        self.conn.commit()
        return cursor.lastrowid
    
    def stop_spam_session(self, account_id: int):
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE active_spam_sessions 
            SET status = 'stopped'
            WHERE account_id = ? AND status = 'running'
        ''', (account_id,))
        self.conn.commit()
    
    def is_spam_running(self, account_id: int):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM active_spam_sessions 
            WHERE account_id = ? AND status = 'running'
        ''', (account_id,))
        return cursor.fetchone()[0] > 0

# ========== СОСТОЯНИЯ ==========
class Form(StatesGroup):
    waiting_for_api_id = State()
    waiting_for_api_hash = State()
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_password = State()
    waiting_for_chats = State()
    waiting_for_general_text = State()
    waiting_for_chat_text = State()
    waiting_for_chat_username = State()

# ========== КЛАСС ДЛЯ УПРАВЛЕНИЯ АККАУНТАМИ ==========
class AccountManager:
    def __init__(self):
        self.active_clients: Dict[int, Client] = {}
        self.spam_tasks: Dict[int, asyncio.Task] = {}
        self.db = Database()
    
    async def add_account(self, user_id: int, api_id: int, api_hash: str, phone_number: str):
        session_name = f"session_{user_id}_{int(time.time())}"
        
        client = Client(session_name, api_id=api_id, api_hash=api_hash)
        
        try:
            await client.start()
            me = await client.get_me()
            
            account_id = self.db.add_telegram_account(
                user_id, session_name, api_id, api_hash, phone_number
            )
            
            self.db.update_account_info(
                account_id, 
                first_name=me.first_name,
                username=me.username,
                is_active=True
            )
            
            await client.stop()
            return account_id, me.first_name, me.username
            
        except Exception as e:
            if client.is_connected:
                await client.stop()
            raise e
    
    def get_account_client(self, account_id: int):
        if account_id not in self.active_clients:
            account = self.db.get_account_info(account_id)
            if not account:
                return None
            
            client = Client(
                account[2],  # session_name
                api_id=account[3],  # api_id
                api_hash=account[4]  # api_hash
            )
            self.active_clients[account_id] = client
        
        return self.active_clients[account_id]
    
    async def start_spam(self, account_id: int, bot: Bot, user_id: int):
        if self.db.is_spam_running(account_id):
            return False
        
        client = self.get_account_client(account_id)
        if not client:
            return False
        
        if not client.is_connected:
            await client.start()
        
        task = asyncio.create_task(self._spam_loop(account_id, client, bot, user_id))
        self.spam_tasks[account_id] = task
        self.db.start_spam_session(account_id)
        return True
    
    async def stop_spam(self, account_id: int):
        if account_id in self.spam_tasks:
            self.spam_tasks[account_id].cancel()
            del self.spam_tasks[account_id]
        
        self.db.stop_spam_session(account_id)
        
        if account_id in self.active_clients:
            try:
                await self.active_clients[account_id].stop()
            except:
                pass
            del self.active_clients[account_id]
    
    async def _spam_loop(self, account_id: int, client: Client, bot: Bot, user_id: int):
        try:
            while True:
                chats = self.db.get_account_chats(account_id)
                general_text = self.db.get_general_text(account_id)
                
                if not chats or not general_text:
                    await bot.send_message(user_id, "❌ Не настроены чаты или текст для рассылки!")
                    break
                
                sent_count = 0
                for chat_info in chats:
                    if not chat_info[2]:  # is_active
                        continue
                    
                    chat_username = chat_info[0]
                    custom_text = chat_info[1]
                    text = custom_text if custom_text else general_text
                    
                    try:
                        await client.send_message(chat_username, text)
                        sent_count += 1
                        await asyncio.sleep(random.randint(5, 15))
                        
                    except FloodWait as e:
                        await bot.send_message(user_id, f"⚠️ FloodWait: ждем {e.value} секунд")
                        await asyncio.sleep(e.value)
                        
                    except Exception as e:
                        logger.error(f"Ошибка отправки в {chat_username}: {e}")
                
                if sent_count > 0:
                    await bot.send_message(user_id, f"✅ Отправлено {sent_count} сообщений. Ждем 60 секунд...")
                
                await asyncio.sleep(60)
                
        except asyncio.CancelledError:
            await bot.send_message(user_id, "🛑 Рассылка остановлена")
        except Exception as e:
            await bot.send_message(user_id, f"❌ Ошибка в рассылке: {e}")

# ========== ИНИЦИАЛИЗАЦИЯ ==========
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
db = Database()
account_manager = AccountManager()

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard(user_id: int):
    accounts = db.get_user_accounts(user_id)
    buttons = []
    
    if accounts:
        for acc in accounts:
            status = "✅" if acc[4] else "❌"
            buttons.append([
                InlineKeyboardButton(
                    f"{status} {acc[2] or 'Без имени'} (@{acc[3] or 'нет'})",
                    callback_data=f"select_account_{acc[0]}"
                )
            ])
    
    buttons.append([
        InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_account_keyboard(account_id: int):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton("✏️ Добавить чаты", callback_data=f"add_chats_{account_id}"),
            InlineKeyboardButton("📝 Текст рассылки", callback_data=f"set_text_{account_id}")
        ],
        [
            InlineKeyboardButton("▶️ Запустить рассылку", callback_data=f"start_spam_{account_id}"),
            InlineKeyboardButton("⏹️ Остановить рассылку", callback_data=f"stop_spam_{account_id}")
        ],
        [
            InlineKeyboardButton("👁️ Просмотреть настройки", callback_data=f"view_settings_{account_id}"),
            InlineKeyboardButton("🗑️ Удалить аккаунт", callback_data=f"delete_account_{account_id}")
        ],
        [
            InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")
        ]
    ])
    return keyboard

def get_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_account")]
    ])

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    db.add_bot_user(user_id)
    
    await message.answer(
        "👋 Добро пожаловать в бота для управления Telegram-рассылкой!\n"
        "Выберите действие:",
        reply_markup=get_main_keyboard(user_id)
    )

@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    help_text = """
📚 **Доступные команды:**
/start - Главное меню
/help - Эта справка

📱 **Управление аккаунтами:**
1. Добавьте аккаунт через кнопку "➕ Добавить аккаунт"
2. Введите API_ID, API_HASH и номер телефона
3. После авторизации аккаунт появится в списке

⚙️ **Настройка рассылки:**
- Добавьте чаты (можно несколько через запятую или пробел)
- Установите текст рассылки
- Для отдельных чатов можно установить особый текст

▶️ **Запуск рассылки:**
- Нажмите "Запустить рассылку" для выбранного аккаунта
- Рассылка будет работать в фоне
- Вы можете остановить ее в любое время
    """
    await message.answer(help_text)

# ========== ОБРАБОТЧИКИ CALLBACK ==========
@dp.callback_query_handler(lambda c: c.data == 'back_to_main')
async def back_to_main(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    user_id = callback_query.from_user.id
    await bot.edit_message_text(
        "👋 Главное меню\nВыберите аккаунт:",
        user_id,
        callback_query.message.message_id,
        reply_markup=get_main_keyboard(user_id)
    )

@dp.callback_query_handler(lambda c: c.data == 'add_account')
async def add_account_start(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    await bot.send_message(
        callback_query.from_user.id,
        "📱 Введите API_ID (получить можно на https://my.telegram.org):"
    )
    await Form.waiting_for_api_id.set()
    await state.update_data(user_id=callback_query.from_user.id)

@dp.callback_query_handler(lambda c: c.data.startswith('select_account_'))
async def select_account(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    account_id = int(callback_query.data.split('_')[2])
    account = db.get_account_info(account_id)
    
    if account:
        db.update_account_info(account_id, is_active=True)
        
        account_info = (
            f"👤 **Аккаунт выбран:**\n"
            f"📛 Имя: {account[6] or 'Не указано'}\n"
            f"🔗 Юзернейм: @{account[7] or 'нет'}\n"
            f"📞 Номер: {account[5] or 'Не указан'}\n"
            f"🆔 API_ID: {account[3]}\n"
            f"📅 Добавлен: {account[9]}"
        )
        
        await bot.edit_message_text(
            account_info,
            callback_query.from_user.id,
            callback_query.message.message_id,
            reply_markup=get_account_keyboard(account_id)
        )

@dp.callback_query_handler(lambda c: c.data.startswith('add_chats_'))
async def add_chats_start(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    account_id = int(callback_query.data.split('_')[2])
    
    chats = db.get_account_chats(account_id)
    if chats:
        chat_list = "\n".join([f"• {c[0]}" for c in chats])
        text = f"📋 Текущие чаты:\n{chat_list}\n\n"
    else:
        text = ""
    
    text += (
        "📝 **Добавление чатов**\n"
        "Отправьте юзернеймы чатов (через @)\n"
        "Можно несколько через запятую или пробел:\n\n"
        "Пример:\n"
        "@chat1 @chat2 @chat3\n"
        "или\n"
        "@chat1, @chat2, @chat3"
    )
    
    await bot.send_message(
        callback_query.from_user.id,
        text,
        reply_markup=get_back_keyboard()
    )
    
    await Form.waiting_for_chats.set()
    await state.update_data(account_id=account_id)

@dp.callback_query_handler(lambda c: c.data.startswith('set_text_'))
async def set_text_start(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    account_id = int(callback_query.data.split('_')[2])
    
    current_text = db.get_general_text(account_id)
    if current_text:
        text = f"📝 Текущий текст:\n{current_text}\n\n"
    else:
        text = ""
    
    text += (
        "📝 **Установка текста рассылки**\n"
        "Отправьте текст, который будет рассылаться:\n\n"
        "Для отдельных чатов можно установить особый текст позже."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton("✏️ Для отдельного чата", callback_data=f"set_chat_text_{account_id}"),
            InlineKeyboardButton("◀️ Назад", callback_data=f"select_account_{account_id}")
        ]
    ])
    
    await bot.send_message(
        callback_query.from_user.id,
        text,
        reply_markup=keyboard
    )
    
    await Form.waiting_for_general_text.set()
    await state.update_data(account_id=account_id)

@dp.callback_query_handler(lambda c: c.data.startswith('set_chat_text_'))
async def set_chat_text_start(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    account_id = int(callback_query.data.split('_')[3])
    
    await bot.send_message(
        callback_query.from_user.id,
        "🔍 Введите юзернейм чата (через @):",
        reply_markup=get_back_keyboard()
    )
    
    await Form.waiting_for_chat_username.set()
    await state.update_data(account_id=account_id)

@dp.callback_query_handler(lambda c: c.data.startswith('start_spam_'))
async def start_spam(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    account_id = int(callback_query.data.split('_')[2])
    user_id = callback_query.from_user.id
    
    chats = db.get_account_chats(account_id)
    general_text = db.get_general_text(account_id)
    
    if not chats:
        await bot.send_message(user_id, "❌ Не добавлены чаты для рассылки!")
        return
    
    if not general_text:
        await bot.send_message(user_id, "❌ Не установлен текст рассылки!")
        return
    
    success = await account_manager.start_spam(account_id, bot, user_id)
    
    if success:
        await bot.send_message(user_id, "✅ Рассылка запущена!")
    else:
        await bot.send_message(user_id, "❌ Рассылка уже запущена или произошла ошибка!")

@dp.callback_query_handler(lambda c: c.data.startswith('stop_spam_'))
async def stop_spam(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    account_id = int(callback_query.data.split('_')[2])
    
    await account_manager.stop_spam(account_id)
    await bot.send_message(callback_query.from_user.id, "🛑 Рассылка остановлена")

@dp.callback_query_handler(lambda c: c.data.startswith('view_settings_'))
async def view_settings(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    account_id = int(callback_query.data.split('_')[2])
    
    account = db.get_account_info(account_id)
    chats = db.get_account_chats(account_id)
    general_text = db.get_general_text(account_id)
    is_running = db.is_spam_running(account_id)
    
    message = f"⚙️ **Настройки аккаунта:**\n\n"
    message += f"👤 Имя: {account[6] or 'Нет'}\n"
    message += f"🔗 Юзернейм: @{account[7] or 'Нет'}\n"
    message += f"📞 Номер: {account[5] or 'Нет'}\n"
    message += f"📊 Статус рассылки: {'▶️ Запущена' if is_running else '⏹️ Остановлена'}\n\n"
    
    message += f"📝 **Общий текст:**\n"
    message += f"{general_text or 'Не установлен'}\n\n"
    
    message += f"📋 **Чаты ({len(chats)}):**\n"
    for i, chat in enumerate(chats[:10], 1):
        status = "✅" if chat[2] else "❌"
        custom = " (особый текст)" if chat[1] else ""
        message += f"{i}. {status} {chat[0]}{custom}\n"
    
    if len(chats) > 10:
        message += f"... и еще {len(chats) - 10} чатов\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton("✏️ Изменить чаты", callback_data=f"add_chats_{account_id}"),
            InlineKeyboardButton("📝 Изменить текст", callback_data=f"set_text_{account_id}")
        ],
        [
            InlineKeyboardButton("◀️ Назад", callback_data=f"select_account_{account_id}")
        ]
    ])
    
    await bot.send_message(
        callback_query.from_user.id,
        message,
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('delete_account_'))
async def delete_account(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    account_id = int(callback_query.data.split('_')[2])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_{account_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data=f"select_account_{account_id}")
        ]
    ])
    
    await bot.edit_message_text(
        "⚠️ **Внимание!**\n"
        "Удаление аккаунта приведет к:\n"
        "• Удалению всех настроек\n"
        "• Удалению списка чатов\n"
        "• Остановке рассылки\n"
        "• Удалению сессии\n\n"
        "Вы уверены?",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('confirm_delete_'))
async def confirm_delete(callback_query: types.CallbackQuery):
    await bot.answer_callback_query(callback_query.id)
    account_id = int(callback_query.data.split('_')[2])
    
    await account_manager.stop_spam(account_id)
    db.delete_account(account_id)
    
    await bot.edit_message_text(
        "✅ Аккаунт удален",
        callback_query.from_user.id,
        callback_query.message.message_id,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("◀️ В главное меню", callback_data="back_to_main")]
        ])
    )

# ========== ОБРАБОТЧИКИ СООБЩЕНИЙ ==========
@dp.message_handler(state=Form.waiting_for_api_id)
async def process_api_id(message: types.Message, state: FSMContext):
    try:
        api_id = int(message.text)
        await state.update_data(api_id=api_id)
        await message.answer("🔑 Введите API_HASH:")
        await Form.waiting_for_api_hash.set()
    except ValueError:
        await message.answer("❌ API_ID должен быть числом! Попробуйте еще раз:")

@dp.message_handler(state=Form.waiting_for_api_hash)
async def process_api_hash(message: types.Message, state: FSMContext):
    api_hash = message.text.strip()
    if len(api_hash) < 10:
        await message.answer("❌ Неверный API_HASH! Попробуйте еще раз:")
        return
    
    await state.update_data(api_hash=api_hash)
    await message.answer("📞 Введите номер телефона (с кодом страны, например +79123456789):")
    await Form.waiting_for_phone.set()

@dp.message_handler(state=Form.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    
    data = await state.get_data()
    api_id = data['api_id']
    api_hash = data['api_hash']
    user_id = data['user_id']
    
    try:
        await message.answer("⏳ Авторизуюсь в Telegram...")
        
        account_id, first_name, username = await account_manager.add_account(
            user_id, api_id, api_hash, phone
        )
        
        await message.answer(
            f"✅ Аккаунт успешно добавлен!\n"
            f"👤 Имя: {first_name}\n"
            f"🔗 Юзернейм: @{username}\n\n"
            f"Теперь вы можете настроить рассылку.",
            reply_markup=get_main_keyboard(user_id)
        )
        
        await state.finish()
        
    except Exception as e:
        await message.answer(f"❌ Ошибка авторизации: {str(e)}\n\nПопробуйте еще раз /start")

@dp.message_handler(state=Form.waiting_for_chats)
async def process_chats(message: types.Message, state: FSMContext):
    data = await state.get_data()
    account_id = data['account_id']
    
    text = message.text.strip()
    usernames = []
    
    for word in text.replace(',', ' ').split():
        word = word.strip()
        if word.startswith('@'):
            usernames.append(word)
        elif word.startswith('https://t.me/'):
            usernames.append('@' + word.split('/')[-1])
    
    if not usernames:
        await message.answer("❌ Не найдено валидных юзернеймов. Попробуйте еще раз:")
        return
    
    db.add_chats(account_id, usernames)
    
    await message.answer(
        f"✅ Добавлено {len(usernames)} чатов:\n" + 
        "\n".join([f"• {u}" for u in usernames]),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("◀️ Назад к аккаунту", callback_data=f"select_account_{account_id}")]
        ])
    )
    
    await state.finish()

@dp.message_handler(state=Form.waiting_for_general_text)
async def process_general_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    account_id = data['account_id']
    
    text = message.text.strip()
    if not text:
        await message.answer("❌ Текст не может быть пустым! Попробуйте еще раз:")
        return
    
    db.set_general_text(account_id, text)
    
    await message.answer(
        "✅ Текст рассылки установлен!\n\n"
        f"📝 Ваш текст:\n{text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("◀️ Назад к аккаунту", callback_data=f"select_account_{account_id}")]
        ])
    )
    
    await state.finish()

@dp.message_handler(state=Form.waiting_for_chat_username)
async def process_chat_username(message: types.Message, state: FSMContext):
    chat_username = message.text.strip()
    if not chat_username.startswith('@'):
        await message.answer("❌ Юзернейм должен начинаться с @. Попробуйте еще раз:")
        return
    
    await state.update_data(chat_username=chat_username)
    await message.answer("📝 Введите текст для этого чата:")
    await Form.waiting_for_chat_text.set()

@dp.message_handler(state=Form.waiting_for_chat_text)
async def process_chat_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    account_id = data['account_id']
    chat_username = data['chat_username']
    
    text = message.text.strip()
    db.update_chat_text(account_id, chat_username, text)
    
    await message.answer(
        f"✅ Текст для {chat_username} установлен!\n\n"
        f"📝 Текст:\n{text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("◀️ Назад к аккаунту", callback_data=f"select_account_{account_id}")]
        ])
    )
    
    await state.finish()

# ========== ЗАПУСК БОТА ==========
async def run_bot():
    logger.info("Запуск Telegram бота...")
    try:
        await dp.start_polling()
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")

def start_bot_in_thread():
    """Запускает бота в отдельном потоке"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_bot())

if __name__ == '__main__':
    # Если запускаем на Render, запускаем в отдельном потоке
    # На обычном сервере просто запускаем
    if os.getenv("RENDER"):
        logger.info("Обнаружен Render, запускаем бота в фоновом потоке")
        bot_thread = Thread(target=start_bot_in_thread, daemon=True)
        bot_thread.start()
        
        # Простой веб-сервер для пинга
        from flask import Flask
        app = Flask(__name__)
        
        @app.route('/')
        def home():
            return "Telegram Bot is running!"
        
        @app.route('/health')
        def health():
            return "OK", 200
        
        port = int(os.getenv("PORT", 8080))
        app.run(host='0.0.0.0', port=port)
    else:
        # Локальный запуск
        executor.start_polling(dp, skip_updates=True)
