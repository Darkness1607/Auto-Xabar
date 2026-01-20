import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from aiogram import F

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DAILY_PRICE = int(os.getenv("DAILY_PRICE", "1000"))
ADMIN_CARD = os.getenv("ADMIN_CARD", "0000 0000 0000 0000")
TAGLINE = os.getenv("TAGLINE", "Auto reklama")

assert API_ID and API_HASH and BOT_TOKEN, "API_ID, API_HASH, BOT_TOKEN .env da berilishi kerak"

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("auto_reklama")

# ===================== BOT INIT =====================
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=storage)

# ===================== STATES =====================
class MsgStates(StatesGroup):
    waiting_text = State()
    waiting_image = State()
    waiting_interval = State()

class GroupStates(StatesGroup):
    waiting_group_pair = State()

class AccountStates(StatesGroup):
    waiting_session = State()

class PaymentStates(StatesGroup):
    waiting_days = State()
    waiting_cheque = State()

class AdminStates(StatesGroup):
    waiting_user_id = State()
    waiting_days_add = State()

# ===================== DB =====================
DB_NAME = "bot.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_admin INTEGER DEFAULT 0,
                paid_until DATETIME,
                balance INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                session_string TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                group_id TEXT,
                group_name TEXT,
                is_active INTEGER DEFAULT 1
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                photo_path TEXT,
                interval_sec INTEGER DEFAULT 60,
                last_sent DATETIME,
                active INTEGER DEFAULT 0,
                sent_count INTEGER DEFAULT 0
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER,
                days INTEGER,
                status TEXT DEFAULT 'pending',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                approved_at DATETIME,
                admin_note TEXT
            )
            """
        )

        await db.commit()

# ===================== HELPERS =====================
async def ensure_user(user_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            is_admin = 1 if user_id == ADMIN_ID else 0
            await db.execute(
                "INSERT INTO users(user_id, is_admin, paid_until, balance) VALUES(?,?,?,?)",
                (user_id, is_admin, datetime.utcnow().isoformat(), 0),
            )
            await db.commit()

async def has_active_sub(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT paid_until FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row or not row[0]:
            return False
        try:
            return datetime.fromisoformat(row[0]) > datetime.utcnow()
        except Exception:
            return False

async def get_user_balance(user_id: int) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0

async def add_user_balance(user_id: int, amount: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id=?",
            (amount, user_id)
        )
        await db.commit()

async def activate_subscription(user_id: int, days: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT paid_until FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        
        now = datetime.utcnow()
        if row and row[0]:
            try:
                current_until = datetime.fromisoformat(row[0])
                if current_until > now:
                    new_until = current_until + timedelta(days=days)
                else:
                    new_until = now + timedelta(days=days)
            except Exception:
                new_until = now + timedelta(days=days)
        else:
            new_until = now + timedelta(days=days)
        
        await db.execute(
            "UPDATE users SET paid_until = ? WHERE user_id=?",
            (new_until.isoformat(), user_id)
        )
        await db.commit()

async def get_user_accounts(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id, phone FROM accounts WHERE user_id=? AND is_active=1",
            (user_id,)
        )
        return await cur.fetchall()

async def get_first_session_string(user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT session_string FROM accounts WHERE user_id=? AND is_active=1 LIMIT 1",
            (user_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else None

async def get_user_groups(user_id: int) -> List[Tuple[int, str, str]]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id, group_id, group_name FROM groups WHERE user_id=? AND is_active=1",
            (user_id,),
        )
        return await cur.fetchall()

async def get_pending_payments() -> List[Tuple]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT p.id, p.user_id, u.balance, p.amount, p.days, p.created_at FROM payments p JOIN users u ON p.user_id = u.user_id WHERE p.status='pending' ORDER BY p.created_at"
        )
        return await cur.fetchall()

async def get_all_users() -> List[Tuple]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT user_id, paid_until, balance, created_at FROM users ORDER BY created_at DESC"
        )
        return await cur.fetchall()

async def save_photo(file_id: str, user_id: int, message_id: int) -> str:
    os.makedirs(f"photos/{user_id}", exist_ok=True)
    file_path = f"photos/{user_id}/{message_id}.jpg"
    file = await bot.get_file(file_id)
    # Aiogram v3: download_file(file_path, destination)
    await bot.download_file(file.file_path, destination=file_path)
    return file_path

# ===================== UI BUILDERS =====================
async def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Akkaunt ulash", callback_data="acc_session")
    kb.button(text="👥 Guruhlar", callback_data="groups")
    kb.button(text="📣 Reklama", callback_data="ads")
    kb.button(text="💳 To'lov", callback_data="pay")
    kb.button(text="ℹ️ Hisob", callback_data="balance")
    if user_id == ADMIN_ID:
        kb.button(text="⚙️ Admin", callback_data="admin")
    kb.adjust(2)
    return kb.as_markup()

async def ads_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🆕 Yangi xabar", callback_data="ads_new")
    kb.button(text="📋 Mening xabarlarim", callback_data="ads_list")
    kb.button(text="◀️ Orqaga", callback_data="back_home")
    kb.adjust(2)
    return kb.as_markup()

async def groups_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Guruh qo'shish", callback_data="group_add")
    kb.button(text="📋 Mening guruhlarim", callback_data="group_list")
    kb.button(text="◀️ Orqaga", callback_data="back_home")
    kb.adjust(2)
    return kb.as_markup()

async def payment_days_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="1 kun - 1,000 so'm", callback_data="pay_days:1")
    kb.button(text="7 kun - 6,000 so'm", callback_data="pay_days:7")
    kb.button(text="30 kun - 25,000 so'm", callback_data="pay_days:30")
    kb.button(text="◀️ Orqaga", callback_data="back_home")
    kb.adjust(1)
    return kb.as_markup()

async def admin_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Statistika", callback_data="admin_stats")
    kb.button(text="💰 To'lovlar", callback_data="admin_payments")
    kb.button(text="👥 Foydalanuvchilar", callback_data="admin_users")
    kb.button(text="➕ Balans qo'shish", callback_data="admin_add_balance")
    kb.button(text="◀️ Orqaga", callback_data="back_home")
    kb.adjust(1)
    return kb.as_markup()

# ===================== COMMANDS =====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await ensure_user(message.from_user.id)
    sub_ok = await has_active_sub(message.from_user.id)
    balance = await get_user_balance(message.from_user.id)

    text = (
        "Assalomu alaykum! 👋\n\n"
        f"<b>Auto Reklama Bot</b> ga xush kelibsiz.\n"
        f"Kunlik narx: <b>{DAILY_PRICE} so'm</b>\n"
        f"Balansingiz: <b>{balance} so'm</b>\n\n"
        "• O'z akkauntingizni ulang (Telethon session string)\n"
        "• O'z guruhlaringizni qo'shing\n"
        "• Reklama xabarini yarating (matn + ixtiyoriy rasm)\n"
        "• Intervalni tanlang va bot avtomatik yuboradi\n\n"
        
    )
    
    if sub_ok:
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("SELECT paid_until FROM users WHERE user_id=?", (message.from_user.id,))
            row = await cur.fetchone()
            until_date = datetime.fromisoformat(row[0]).strftime("%Y-%m-%d %H:%M")
        text += f"\n✅ <b>Obuna aktiv</b> (tugash: {until_date})"
    else:
        text += "\n❗️ <b>Obuna aktiv emas.</b> To'lov qiling."

    await message.answer(text, reply_markup=await main_menu_kb(message.from_user.id))

# ===================== PAYMENT FLOW =====================
@dp.callback_query(F.data == "pay")
async def pay_menu(cb: types.CallbackQuery):
    balance = await get_user_balance(cb.from_user.id)
    await cb.message.edit_text(
        f"💳 To'lov qilish\n\n"
        f"Balansingiz: <b>{balance} so'm</b>\n"
        f"Kunlik narx: <b>{DAILY_PRICE} so'm</b>\n\n"
        "Necha kunlik obuna sotib olmoqchisiz?",
        reply_markup=await payment_days_kb()
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("pay_days:"))
async def pay_days_select(cb: types.CallbackQuery, state: FSMContext):
    days = int(cb.data.split(":")[1])
    amount = days * DAILY_PRICE
    
    await state.update_data(days=days, amount=amount)
    await state.set_state(PaymentStates.waiting_cheque)
    
    await cb.message.edit_text(
        f"💳 To'lov qilish\n\n"
        f"Kunlar: <b>{days} kun</b>\n"
        f"Summa: <b>{amount} so'm</b>\n\n"
        f"To'lov qilish uchun karta: <code>{ADMIN_CARD}</code>\n\n"
        "To'lov qilib bo'lgach, chek rasmini yuboring.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ Orqaga", callback_data="pay")]]
        )
    )
    await cb.answer()

@dp.message(PaymentStates.waiting_cheque, F.photo)
async def payment_cheque_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    days = data.get('days')
    amount = data.get('amount')
    
    try:
        photo_id = message.photo[-1].file_id
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO payments (user_id, amount, days, status) VALUES (?, ?, ?, ?)",
                (message.from_user.id, amount, days, 'pending')
            )
            await db.commit()
        
        # Admin ga xabar
        admin_text = (
            f"💰 Yangi to'lov so'rovi\n\n"
            f"User: <code>{message.from_user.id}</code>\n"
            f"Ism: {message.from_user.first_name}\n"
            f"Kunlar: {days}\n"
            f"Summa: {amount} so'm\n\n"
            f"Tasdiqlash: /approve_{message.from_user.id}_{days}\n"
            f"Rad etish: /reject_{message.from_user.id}"
        )
        
        await bot.send_photo(
            ADMIN_ID,
            photo=photo_id,
            caption=admin_text
        )
        
        await message.answer(
            "✅ To'lov cheki qabul qilindi. Admin tasdiqlashini kuting.\n"
            "Tasdiqlangandan so'ng obunangiz faollashtiriladi."
        )
        
    except Exception as e:
        await message.answer("❌ Xatolik yuz berdi. Qayta urinib ko'ring.")
        logger.error(f"Payment error: {e}")
    
    await state.clear()

# ===== Admin dynamic command handlers (regex) =====
@dp.message(F.text.regexp(r"^/approve_(\d+)_(\d+)$"))
async def admin_approve_payment(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.split('_')
        user_id = int(parts[1])
        days = int(parts[2])

        # Obunani faollashtirish
        await activate_subscription(user_id, days)

        # Payment statusini yangilash
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "UPDATE payments SET status='approved', approved_at=?, admin_note=? WHERE user_id=? AND status='pending'",
                (datetime.utcnow().isoformat(), f"Approved by admin {message.from_user.id}", user_id)
            )
            await db.commit()
        
        await message.answer(f"✅ {user_id} foydalanuvchining obunasi {days} kunga faollashtirildi.")
        
        # Foydalanuvchiga xabar
        try:
            async with aiosqlite.connect(DB_NAME) as db:
                cur = await db.execute("SELECT paid_until FROM users WHERE user_id=?", (user_id,))
                row = await cur.fetchone()
            until_text = datetime.fromisoformat(row[0]).strftime("%Y-%m-%d %H:%M") if row and row[0] else "-"
            await bot.send_message(
                user_id,
                f"✅ To'lovingiz tasdiqlandi! Obunangiz {days} kunga faollashtirildi.\n"
                f"Tugash sanasi: {until_text}"
            )
        except Exception:
            pass
            
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")

@dp.message(F.text.regexp(r"^/reject_(\d+)$"))
async def admin_reject_payment(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        user_id = int(message.text.split('_')[1])
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "UPDATE payments SET status='rejected', admin_note=? WHERE user_id=? AND status='pending'",
                (f"Rejected by admin {message.from_user.id}", user_id)
            )
            await db.commit()
        
        await message.answer(f"❌ {user_id} foydalanuvchining to'lovi rad etildi.")
        
        # Foydalanuvchiga xabar
        try:
            await bot.send_message(
                user_id,
                "❌ To'lovingiz rad etildi. Admin bilan bog'laning."
            )
        except Exception:
            pass
            
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")

@dp.message(F.text.regexp(r"^/addbalance_(\d+)_(\d+)$"))
async def admin_add_balance_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        _, user_id_s, amount_s = message.text.split('_')
        user_id = int(user_id_s)
        amount = int(amount_s)
        await add_user_balance(user_id, amount)
        await message.answer(f"✅ {user_id} foydalanuvchiga {amount} so'm qo'shildi.")
        try:
            new_balance = await get_user_balance(user_id)
            await bot.send_message(
                user_id,
                f"💰 Admin sizning balansingizga {amount} so'm qo'shdi.\nYangi balans: {new_balance} so'm"
            )
        except Exception:
            pass
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")

# ===================== ADMIN PANEL =====================
@dp.callback_query(F.data == "admin")
async def admin_panel(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Ruxsat yo'q", show_alert=True)
        return
    
    async with aiosqlite.connect(DB_NAME) as db:
        users_count = await (await db.execute("SELECT COUNT(*) FROM users")).fetchone()
        active_users = await (await db.execute("SELECT COUNT(*) FROM users WHERE paid_until > ?", (datetime.utcnow().isoformat(),))).fetchone()
        payments_count = await (await db.execute("SELECT COUNT(*) FROM payments WHERE status='approved'")) .fetchone()
        total_income = await (await db.execute("SELECT SUM(amount) FROM payments WHERE status='approved'")) .fetchone()
    
    text = (
        "⚙️ <b>Admin Panel</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{users_count[0]}</b>\n"
        f"✅ Faol obunalar: <b>{active_users[0]}</b>\n"
        f"💰 To'lovlar: <b>{payments_count[0]}</b>\n"
        f"💵 Jami daromad: <b>{total_income[0] or 0} so'm</b>\n\n"
        "Quyidagi amallarni bajarishingiz mumkin:"
    )
    
    await cb.message.edit_text(text, reply_markup=await admin_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Ruxsat yo'q", show_alert=True)
        return
    
    async with aiosqlite.connect(DB_NAME) as db:
        today = datetime.utcnow().date().isoformat()
        daily_payments = await (await db.execute(
            "SELECT COUNT(*), SUM(amount) FROM payments WHERE date(approved_at)=? AND status='approved'",
            (today,)
        )).fetchone()
        
        weekly_payments = await (await db.execute(
            "SELECT COUNT(*), SUM(amount) FROM payments WHERE date(approved_at) >= date('now', '-7 days') AND status='approved'"
        )).fetchone()
    
    text = (
        "📊 <b>Batafsil Statistika</b>\n\n"
        f"📅 Bugungi to'lovlar: <b>{daily_payments[0] or 0}</b> ta, {daily_payments[1] or 0} so'm\n"
        f"📈 Haftalik to'lovlar: <b>{weekly_payments[0] or 0}</b> ta, {weekly_payments[1] or 0} so'm\n\n"
        "Foydali buyruqlar:\n"
        "/stats - To'liq statistika\n"
        "/users - Foydalanuvchilar ro'yxati\n"
        "/payments - To'lovlar tarixi"
    )
    
    await cb.message.edit_text(text, reply_markup=await admin_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_payments")
async def admin_payments_list(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Ruxsat yo'q", show_alert=True)
        return
    
    payments = await get_pending_payments()
    
    if not payments:
        text = "📭 Hozircha kutayotgan to'lovlar yo'q."
    else:
        text = "💰 <b>Kutayotgan To'lovlar</b>\n\n"
        for pid, user_id, balance, amount, days, created in payments:
            text += f"👤 {user_id} | {amount}so'm ({days}kun) | Balans: {balance}so'm\n"
            text += f"⏰ {created}\n"
            text += f"✅ /approve_{user_id}_{days} | ❌ /reject_{user_id}\n\n"
    
    await cb.message.edit_text(text, reply_markup=await admin_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_users")
async def admin_users_list(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Ruxsat yo'q", show_alert=True)
        return
    
    users = await get_all_users()
    text = "👥 <b>Foydalanuvchilar Ro'yxati</b>\n\n"
    
    # Faqat 10 ta ko'rsatamiz
    for user_id, paid_until, balance, created in users[:10]:
        status = "✅ Faol" if await has_active_sub(user_id) else "❌ Nofaol"
        text += f"👤 {user_id} | {status} | {balance}so'm\n"
        text += f"📅 {created}\n\n"
    
    if len(users) > 10:
        text += f"... va yana {len(users) - 10} ta foydalanuvchi"
    
    await cb.message.edit_text(text, reply_markup=await admin_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_add_balance")
async def admin_add_balance_menu(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Ruxsat yo'q", show_alert=True)
        return
    
    await cb.message.edit_text(
        "➕ Balans qo'shish\n\nFoydalanuvchi ID sini yuboring:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ Orqaga", callback_data="admin")]]
        )
    )
    await state.set_state(AdminStates.waiting_user_id)
    await cb.answer()

@dp.message(AdminStates.waiting_user_id)
async def admin_user_id_received(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        await state.update_data(user_id=user_id)
        await state.set_state(AdminStates.waiting_days_add)
        await message.answer("💳 Qancha so'm qo'shmoqchisiz?")
    except ValueError:
        await message.answer("❌ Noto'g'ri ID. Faqat raqam kiriting.")

@dp.message(AdminStates.waiting_days_add)
async def admin_amount_received(message: types.Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
        data = await state.get_data()
        user_id = data.get('user_id')
        
        await add_user_balance(user_id, amount)
        
        await message.answer(
            f"✅ {user_id} foydalanuvchiga {amount} so'm qo'shildi.\n"
            f"Yangi balans: {await get_user_balance(user_id)} so'm"
        )
        
        try:
            await bot.send_message(
                user_id,
                f"💰 Admin sizning balansingizga {amount} so'm qo'shdi.\nYangi balans: {await get_user_balance(user_id)} so'm"
            )
        except Exception:
            pass
            
    except ValueError:
        await message.answer("❌ Noto'g'ri summa. Faqat raqam kiriting.")
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")
    finally:
        await state.clear()

# ===================== BALANCE CHECK =====================
@dp.callback_query(F.data == "balance")
async def check_balance(cb: types.CallbackQuery):
    balance = await get_user_balance(cb.from_user.id)
    sub_ok = await has_active_sub(cb.from_user.id)
    
    text = (
        f"💰 <b>Hisobingiz</b>\n\n"
        f"Balans: <b>{balance} so'm</b>\n"
        f"Kunlik narx: <b>{DAILY_PRICE} so'm</b>\n"
        f"Status: {'✅ Faol' if sub_ok else '❌ Nofaol'}\n\n"
    )
    
    if sub_ok:
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("SELECT paid_until FROM users WHERE user_id=?", (cb.from_user.id,))
            row = await cur.fetchone()
            until_date = datetime.fromisoformat(row[0]).strftime("%Y-%m-%d %H:%M")
        text += f"Obuna tugashi: {until_date}"
    else:
        text += "Obuna aktivlashtirish uchun to'lov qiling."
    
    await cb.message.edit_text(text, reply_markup=await main_menu_kb(cb.from_user.id))
    await cb.answer()

# ===================== ACCOUNT FLOW =====================
@dp.callback_query(F.data == "acc_session")
async def account_menu(cb: types.CallbackQuery, state: FSMContext):
    accounts = await get_user_accounts(cb.from_user.id)

    text = "🔗 <b>Telegram Akkauntlar</b>\n\n"

    kb = InlineKeyboardBuilder()

    if accounts:
        text += "Ulangan akkauntlar:\n"
        for acc_id, phone in accounts:
            text += f"• +{phone}\n"
            kb.button(
                text=f"❌ O‘chirish +{phone}",
                callback_data=f"acc_del:{acc_id}"
            )
    else:
        text += "Hozircha akkaunt ulanmagan.\n"

    kb.button(text="◀️ Orqaga", callback_data="back_home")
    kb.adjust(1)

    await cb.message.edit_text(
        text,
        reply_markup=kb.as_markup()
    )
    await cb.answer()
    await cb.message.answer(
        "Telethon session string yuboring:\n\n"
        "Session string olish uchun @sessionuz_bot ga boring va /start buyrug'ini yuboring.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Session olish", url="https://t.me/sessionuz_bot")],
                [InlineKeyboardButton(text="◀️ Bekor qilish", callback_data="back_home")]
            ]
        )
    )
    await state.set_state(AccountStates.waiting_session)

@dp.message(AccountStates.waiting_session)
async def session_received(message: types.Message, state: FSMContext):  # state parametrini qo'shing
    session_string = message.text.strip()
    
    try:
        client = TelegramClient(
            StringSession(session_string),
            API_ID,
            API_HASH
        )
        
        await client.start()
        me = await client.get_me()
        phone = me.phone or "unknown"
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO accounts (user_id, phone, session_string) VALUES (?, ?, ?)",
                (message.from_user.id, phone, session_string)
            )
            await db.commit()
        
        await message.answer(
            f"✅ Akkaunt muvaffaqiyatli ulandi!\nTelefon: +{phone}"
        )
        await client.disconnect()
        
    except SessionPasswordNeededError:
        await message.answer(
            "❌ Akkaunt 2-qadamli autentifikatsiya bilan himoyalangan.\n"
            "Boshqa akkaunt ulashing yoki himoyani vaqtincha o'chiring."
        )
    except Exception as e:
        await message.answer(
            f"❌ Xatolik: {str(e)}\nSession string noto'g'ri yoki muddati o'tgan."
        )
    
    await state.clear()

@dp.callback_query(F.data.startswith("acc_del:"))
async def delete_account(cb: types.CallbackQuery):
    acc_id = int(cb.data.split(":")[1])
    user_id = cb.from_user.id

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE accounts SET is_active=0 WHERE id=? AND user_id=?",
            (acc_id, user_id)
        )
        await db.commit()

    await cb.answer("Akkaunt o‘chirildi ✅", show_alert=True)

    # Ro‘yxatni yangilab ko‘rsatamiz
    await account_menu(cb, None)

# ===================== GROUPS FLOW =====================
@dp.callback_query(F.data == "groups")
async def groups_menu(cb: types.CallbackQuery):
    groups = await get_user_groups(cb.from_user.id)
    
    text = "👥 <b>Guruhlar</b>\n\n"
    if groups:
        text += "Qo'shilgan guruhlar:\n"
        for _, gr_id_str, gr_name in groups:
            text += f"• {gr_name} (<code>{gr_id_str}</code>)\n"
    else:
        text += "Hozircha guruh qo'shilmagan.\n"
    
    await cb.message.edit_text(text, reply_markup=await groups_menu_kb())
    await cb.answer()

@dp.callback_query(F.data == "group_add")
async def group_add_start(cb: types.CallbackQuery, state: FSMContext):
    accounts = await get_user_accounts(cb.from_user.id)
    
    if not accounts:
        await cb.message.edit_text(
            "❌ Avval akkaunt ulashingiz kerak!",
            reply_markup=await groups_menu_kb()
        )
        await cb.answer()
        return
    
    await cb.message.edit_text(
        "Guruh qo'shish uchun:\n\n"
        "1. Akkauntingiz guruhga qo'shilgan bo'lsin (yaxshisi admin).\n"
        "2. Guruh identifikatorini yuboring.\n\n"
        "Identifikator: @username yoki -100123456789 formatida",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Orqaga", callback_data="groups")]
            ]
        )
    )
    await state.set_state(GroupStates.waiting_group_pair)
    await cb.answer()

@dp.message(GroupStates.waiting_group_pair)
async def group_id_received(message: types.Message, state: FSMContext):
    group_identifier = message.text.strip()
    
    try:
        session = await get_first_session_string(message.from_user.id)
        if not session:
            await message.answer("❌ Faol akkaunt topilmadi. Avval akkaunt ulang.")
            await state.clear()
            return
        
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.start()
        entity = await client.get_entity(group_identifier)
        title = getattr(entity, 'title', None) or getattr(entity, 'username', None) or str(entity.id)
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO groups (user_id, group_id, group_name) VALUES (?, ?, ?)",
                (message.from_user.id, group_identifier, title)
            )
            await db.commit()
        
        await message.answer(
            f"✅ Guruh qo'shildi: {title}"
        )
        await client.disconnect()
        
    except Exception as e:
        await message.answer(
            f"❌ Xatolik: {str(e)}\n"
            "Guruh ID/username noto'g'ri yoki akkaunt guruhga qo'shilmagan."
        )
    
    await state.clear()

@dp.callback_query(F.data == "group_list")
async def group_list(cb: types.CallbackQuery):
    groups = await get_user_groups(cb.from_user.id)
    
    text = "📋 <b>Mening Guruhlarim</b>\n\n"
    if not groups:
        text += "Hozircha guruh qo'shilmagan."
    else:
        for _, gr_id_str, gr_name in groups:
            text += f"• {gr_name} (<code>{gr_id_str}</code>)\n"
    
    await cb.message.edit_text(text, reply_markup=await groups_menu_kb())
    await cb.answer()

# ===================== ADS FLOW =====================
@dp.callback_query(F.data == "ads")
async def ads_menu(cb: types.CallbackQuery):
    await cb.message.edit_text(
        "📣 <b>Reklama Xabarlari</b>\n\n"
        "Xabarlaringizni boshqarishingiz mumkin:",
        reply_markup=await ads_menu_kb()
    )
    await cb.answer()

@dp.callback_query(F.data == "ads_new")
async def ads_new_start(cb: types.CallbackQuery, state: FSMContext):
    # Obuna tekshiruvi (ixtiyoriy qat'iylashtirish)
    if not await has_active_sub(cb.from_user.id):
        await cb.answer("Obuna talab qilinadi", show_alert=True)
        return
    await cb.message.edit_text(
        "✍️ Yangi reklama yaratish\n\nMatn yuboring:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ Orqaga", callback_data="ads")]]
        )
    )
    await state.set_state(MsgStates.waiting_text)
    await cb.answer()

@dp.message(MsgStates.waiting_text)
async def ads_text_received(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text)
    await state.set_state(MsgStates.waiting_image)
    await message.answer(
        "📷 Agar rasm qo‘shmoqchi bo‘lsangiz yuboring.\n\n"
        "Aks holda «/skip» yozing."
    )

@dp.message(MsgStates.waiting_image, F.photo)
async def ads_image_received(message: types.Message, state: FSMContext):
    file_path = await save_photo(message.photo[-1].file_id, message.from_user.id, message.message_id)
    await state.update_data(photo=file_path)
    await state.set_state(MsgStates.waiting_interval)
    await message.answer("⏱ Necha soniyada bir yuborilsin? (masalan: 60)")

@dp.message(MsgStates.waiting_image, F.text == "/skip")
async def ads_skip_image(message: types.Message, state: FSMContext):
    await state.update_data(photo=None)
    await state.set_state(MsgStates.waiting_interval)
    await message.answer("⏱ Necha soniyada bir yuborilsin? (masalan: 60)")

@dp.message(MsgStates.waiting_interval)
async def ads_interval_received(message: types.Message, state: FSMContext):
    try:
        interval = int(message.text.strip())
        if interval < 15:
            await message.answer("❌ Interval juda kichik. Kamida 15 soniya kiriting.")
            return
    except ValueError:
        await message.answer("❌ Faqat son kiriting. Masalan: 60")
        return
    
    data = await state.get_data()
    text = data.get("text")
    photo = data.get("photo")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO messages (user_id, text, photo_path, interval_sec, active) VALUES (?,?,?,?,?)",
            (message.from_user.id, text, photo, interval, 1)
        )
        await db.commit()
    
    await message.answer(
        "✅ Reklama saqlandi va faollashtirildi.\n"
        f"Interval: {interval} soniya\n"
        "Bot avtomatik yuboradi."
    )
    await state.clear()

@dp.callback_query(F.data == "ads_list")
async def ads_list(cb: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id, text, interval_sec, sent_count, active FROM messages WHERE user_id=?",
            (cb.from_user.id,)
        )
        rows = await cur.fetchall()
    
    if not rows:
        text = "📭 Hozircha reklama xabarlari yo‘q."
    else:
        text = "📋 <b>Mening Reklamalarim</b>\n\n"
        for mid, msg_text, interval, sent, active in rows:
            status = "✅ Aktiv" if active else "⏸ To‘xtatilgan"
            short = (msg_text[:40] + '...') if len(msg_text) > 43 else msg_text
            text += (
                f"ID: {mid}\n"
                f"Matn: {short}\n"
                f"Interval: {interval} soniya\n"
                f"Yuborilgan: {sent} marta\n"
                f"Holat: {status}\n\n"
            )
    
    await cb.message.edit_text(text, reply_markup=await ads_menu_kb())
    await cb.answer()

# ===================== BACK BUTTONS =====================
@dp.callback_query(F.data == "back_home")
async def back_to_home(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cmd_start(cb.message)
    await cb.answer()

# ===================== WORKER: ADS SENDER =====================
async def send_ad_with_session(session: str, target: str, text: str, photo: Optional[str]):
    client = TelegramClient(StringSession(session), API_ID, API_HASH)
    await client.start()
    try:
        # target bu @username yoki -100... string holida saqlanadi
        if photo:
            await client.send_file(target, file=photo, caption=text)
        else:
            await client.send_message(target, text)
    finally:
        await client.disconnect()

async def ads_worker():
    logger.info("Ads worker started")
    while True:
        try:
            # Har 8 soniyada tekshirib turamiz
            async with aiosqlite.connect(DB_NAME) as db:
                cur = await db.execute(
                    "SELECT id, user_id, text, photo_path, interval_sec, last_sent FROM messages WHERE active=1"
                )
                ads = await cur.fetchall()
            
            for msg_id, user_id, text, photo, interval, last_sent in ads:
                # Obuna tekshiruvi: faqat aktiv obunasi bo'lganlar uchun yuboramiz
                if not await has_active_sub(user_id):
                    continue
                # vaqt tekshiruvi
                if last_sent:
                    try:
                        last_dt = datetime.fromisoformat(last_sent)
                        if datetime.utcnow() - last_dt < timedelta(seconds=interval):
                            continue
                    except Exception:
                        pass
                
                groups = await get_user_groups(user_id)
                if not groups:
                    continue
                session = await get_first_session_string(user_id)
                if not session:
                    continue
                
                full_text = f"{text}\n\n{TAGLINE}" if TAGLINE else text
                # Bitta sessiya bilan ketma-ket yuboramiz
                for _, group_ref, _ in groups:
                    try:
                        await send_ad_with_session(session, group_ref, full_text, photo)
                        await asyncio.sleep(0.5)  # spamlashni kamaytirish
                    except FloodWaitError as e:
                        logger.warning(f"FloodWait: {e}")
                        await asyncio.sleep(e.seconds)
                    except Exception as e:
                        logger.error(f"Send error to {group_ref}: {e}")
                
                # Bazada oxirgi yuborilgan vaqtni yangilash
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute(
                        "UPDATE messages SET last_sent=?, sent_count=sent_count+1 WHERE id=?",
                        (datetime.utcnow().isoformat(), msg_id)
                    )
                    await db.commit()
        except Exception as e:
            logger.error(f"Worker loop error: {e}")
        await asyncio.sleep(8)

# ===================== MAIN =====================
async def main():
    await init_db()
    # Worker ishga tushadi
    asyncio.create_task(ads_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
