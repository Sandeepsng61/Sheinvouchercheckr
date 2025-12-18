#!/usr/bin/env python3
"""
made with love by shivang
replace your bot token
"""
from __future__ import annotations
import json
import asyncio
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set
import requests
import aiohttp
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters, ConversationHandler, CallbackQueryHandler
)

# ---------------------------
# Configuration
# ---------------------------
logging.basicConfig(level=logging.INFO)
TIMEOUT_SHEIN = 20
TIMEOUT_VOUCHER = 10
CONCURRENCY_LIMIT = 8
PROTECT_INTERVAL = 600  # 10 minutes in seconds
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
BOT_TOKEN = "8546609232:AAEd6DRKe7648Se3GmwsHF3pZ8u1By9DdyU"
DEFAULT_PRODUCT_ID = "443323084003"
DEFAULT_QUANTITY = 1
DEFAULT_SIZES = ["32", "30", "M", "L"]
DESIRED_ORDER = [
    "V", "LS", "EI", "mE", "mN", "uI", "un", "MN", "CI", "PK",
    "SN", "G", "A", "U", "R", "C", "M", "GUID", "bookingType"
]
DB_FILE = 'users.db'

# Global data structures
user_data: Dict[int, Dict[str, Any]] = {}
protect_tasks: Dict[int, asyncio.Task] = {}
protect_vouchers: Dict[int, List[str]] = {}
protect_status: Dict[int, Dict[str, Any]] = {}

# Conversation states
PHONE, OTP = range(2)

# ---------------------------
# Database Management
# ---------------------------
def init_db():
    """Initialize SQLite database."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, cookie TEXT, phone TEXT)''')
    conn.commit()
    conn.close()

def save_cookie_to_db(user_id: int, cookie: str, phone: str):
    """Save or update user's cookie and phone in DB."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, cookie, phone) VALUES (?, ?, ?)",
              (user_id, cookie, phone))
    conn.commit()
    conn.close()

def get_cookie_from_db(user_id: int) -> Optional[str]:
    """Load user's cookie from DB."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT cookie FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def initialize_user_data(user_id: int):
    """Initialize user data if not present."""
    if user_id not in user_data:
        user_data[user_id] = {
            'product_id': DEFAULT_PRODUCT_ID,
            'quantity': DEFAULT_QUANTITY,
            'sizes': DEFAULT_SIZES,
        }
        cookie = get_cookie_from_db(user_id)
        if cookie:
            user_data[user_id]['cookie'] = cookie

# ---------------------------
# Voucher Protector System
# ---------------------------
async def protect_voucher_task(user_id: int, voucher: str, context: ContextTypes.DEFAULT_TYPE):
    """Background task to protect a voucher by re-applying every 10 minutes."""
    config = user_data.get(user_id)
    if not config or not config.get('cookie'):
        logging.error(f"User {user_id} has no cookie for protection")
        return
    
    cookie = config['cookie']
    
    # Initialize status
    if user_id not in protect_status:
        protect_status[user_id] = {}
    
    protect_status[user_id][voucher] = {
        'status': 'active',
        'start_time': datetime.now(),
        'last_apply': None,
        'apply_count': 0,
        'errors': 0
    }
    
    logging.info(f"Starting protection for voucher {voucher} for user {user_id}")
    
    try:
        while user_id in protect_vouchers and voucher in protect_vouchers[user_id]:
            try:
                # Apply voucher
                success, amount = await apply_voucher_protect(cookie, voucher)
                
                if success:
                    protect_status[user_id][voucher]['last_apply'] = datetime.now()
                    protect_status[user_id][voucher]['apply_count'] += 1
                    protect_status[user_id][voucher]['errors'] = 0
                    
                    value = get_voucher_value(voucher)
                    logging.info(f"Protected voucher {voucher} applied successfully (Value: ₹{value})")
                    
                    # Send success notification (only first time and every 5th time)
                    if protect_status[user_id][voucher]['apply_count'] == 1 or \
                       protect_status[user_id][voucher]['apply_count'] % 5 == 0:
                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=f"🛡️ *Voucher Protected*\n\n"
                                     f"✅ Voucher: `{voucher}`\n"
                                     f"💰 Value: ₹{value}\n"
                                     f"🔄 Applied: {protect_status[user_id][voucher]['apply_count']} times\n"
                                     f"⏰ Next apply: 10 minutes\n\n"
                                     f"*Status:* Active ✅",
                                parse_mode="Markdown"
                            )
                        except:
                            pass
                else:
                    protect_status[user_id][voucher]['errors'] += 1
                    logging.warning(f"Failed to apply protected voucher {voucher}")
                    
                    # If too many errors, stop protection
                    if protect_status[user_id][voucher]['errors'] >= 3:
                        logging.error(f"Too many errors for voucher {voucher}, stopping protection")
                        if user_id in protect_vouchers and voucher in protect_vouchers[user_id]:
                            protect_vouchers[user_id].remove(voucher)
                        if user_id in protect_status and voucher in protect_status[user_id]:
                            protect_status[user_id][voucher]['status'] = 'error'
                        
                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=f"❌ *Protection Stopped*\n\n"
                                     f"Voucher: `{voucher}`\n"
                                     f"Reason: Too many application errors\n"
                                     f"Please check your cookie/session.",
                                parse_mode="Markdown"
                            )
                        except:
                            pass
                        break
                
            except Exception as e:
                logging.error(f"Error in protect task for voucher {voucher}: {e}")
                protect_status[user_id][voucher]['errors'] += 1
            
            # Wait for 10 minutes
            for _ in range(PROTECT_INTERVAL):
                if user_id not in protect_vouchers or voucher not in protect_vouchers[user_id]:
                    break
                await asyncio.sleep(1)
    
    except asyncio.CancelledError:
        logging.info(f"Protection task for voucher {voucher} cancelled")
    except Exception as e:
        logging.error(f"Protection task error for voucher {voucher}: {e}")
    finally:
        # Cleanup
        if user_id in protect_status and voucher in protect_status[user_id]:
            protect_status[user_id][voucher]['status'] = 'stopped'
            protect_status[user_id][voucher]['end_time'] = datetime.now()
        
        logging.info(f"Protection stopped for voucher {voucher}")

async def apply_voucher_protect(cookie: str, voucher: str) -> Tuple[bool, int]:
    """Apply voucher for protection system."""
    session = create_aio_session(cookie)
    try:
        # First reset any existing voucher
        await reset_voucher(session, voucher)
        
        # Apply the voucher
        async with session.post(
            "https://www.sheinindia.in/api/cart/apply-voucher",
            json={"voucherId": voucher, "device": {"client_type": "MSITE"}}
        ) as resp:
            if resp.status == 200:
                resp_data = await resp.json()
                voucher_amount = resp_data.get("voucherAmount", {}).get("value", 0)
                applied_vouchers = resp_data.get("appliedVouchers", [])
                for applied in applied_vouchers:
                    if applied.get("voucherCode") == voucher or applied.get("code") == voucher:
                        return True, voucher_amount
                if voucher_amount > 0:
                    return True, voucher_amount
    except Exception as e:
        logging.error(f"Error applying voucher {voucher} in protect mode: {e}")
    finally:
        await session.close()
    return False, 0

async def start_voucher_protection(user_id: int, vouchers: List[str], context: ContextTypes.DEFAULT_TYPE):
    """Start protection for multiple vouchers."""
    if not vouchers:
        return False
    
    if user_id not in protect_vouchers:
        protect_vouchers[user_id] = []
    
    added_vouchers = []
    for voucher in vouchers:
        if voucher not in protect_vouchers[user_id]:
            protect_vouchers[user_id].append(voucher)
            added_vouchers.append(voucher)
            
            # Start protection task
            task = asyncio.create_task(protect_voucher_task(user_id, voucher, context))
            if user_id not in protect_tasks:
                protect_tasks[user_id] = {}
            protect_tasks[user_id][voucher] = task
    
    return len(added_vouchers) > 0

async def stop_voucher_protection(user_id: int, voucher: str = None):
    """Stop protection for voucher(s)."""
    if user_id not in protect_vouchers:
        return False
    
    if voucher:
        # Stop specific voucher
        if voucher in protect_vouchers[user_id]:
            protect_vouchers[user_id].remove(voucher)
            
            # Cancel task if exists
            if user_id in protect_tasks and voucher in protect_tasks[user_id]:
                protect_tasks[user_id][voucher].cancel()
                del protect_tasks[user_id][voucher]
            
            # Update status
            if user_id in protect_status and voucher in protect_status[user_id]:
                protect_status[user_id][voucher]['status'] = 'stopped'
                protect_status[user_id][voucher]['end_time'] = datetime.now()
            
            return True
    else:
        # Stop all vouchers for user
        vouchers_to_stop = protect_vouchers[user_id].copy()
        for v in vouchers_to_stop:
            if user_id in protect_tasks and v in protect_tasks[user_id]:
                protect_tasks[user_id][v].cancel()
                del protect_tasks[user_id][v]
            
            if user_id in protect_status and v in protect_status[user_id]:
                protect_status[user_id][v]['status'] = 'stopped'
                protect_status[user_id][v]['end_time'] = datetime.now()
        
        protect_vouchers[user_id].clear()
        return True
    
    return False

def get_protection_status(user_id: int) -> str:
    """Get protection status summary."""
    if user_id not in protect_vouchers or not protect_vouchers[user_id]:
        return "❌ No active protections"
    
    status_lines = []
    for voucher in protect_vouchers[user_id]:
        if user_id in protect_status and voucher in protect_status[user_id]:
            status = protect_status[user_id][voucher]
            value = get_voucher_value(voucher)
            
            if status['last_apply']:
                last_apply_str = status['last_apply'].strftime("%H:%M:%S")
                next_apply = status['last_apply'] + timedelta(seconds=PROTECT_INTERVAL)
                next_apply_str = next_apply.strftime("%H:%M:%S")
                
                time_until_next = next_apply - datetime.now()
                if time_until_next.total_seconds() > 0:
                    minutes = int(time_until_next.total_seconds() // 60)
                    seconds = int(time_until_next.total_seconds() % 60)
                    next_str = f"{minutes}m {seconds}s"
                else:
                    next_str = "Soon"
            else:
                last_apply_str = "Never"
                next_str = "Soon"
            
            status_lines.append(
                f"• `{voucher}` - ₹{value}\n"
                f"  ↳ Status: {status['status'].upper()}\n"
                f"  ↳ Applied: {status['apply_count']} times\n"
                f"  ↳ Last: {last_apply_str}\n"
                f"  ↳ Next: {next_str}"
            )
        else:
            status_lines.append(f"• `{voucher}` - Status unknown")
    
    return "\n\n".join(status_lines)

# ---------------------------
# Shein API Logic (Sync with requests)
# ---------------------------
def build_headers_shein(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build headers for Shein API calls."""
    base = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": USER_AGENT,
        "Referer": "https://www.sheinindia.in/",
        "X-Requested-With": "XMLHttpRequest",
        "X-TENANT-ID": "SHEIN",
        "Accept-Language": "en-IN,en;q=0.9",
        "Connection": "keep-alive",
    }
    if extra:
        base.update(extra)
    return base

def check_account(session: requests.Session, mobile_number: str) -> Dict[str, Any]:
    url = 'https://www.sheinindia.in/api/auth/accountCheck'
    data = {"mobileNumber": mobile_number}
    response = session.post(url, json=data, headers=build_headers_shein(), timeout=TIMEOUT_SHEIN)
    response.raise_for_status()
    return response.json()

def generate_otp(session: requests.Session, mobile_number: str) -> Dict[str, Any]:
    url = 'https://www.sheinindia.in/api/auth/generateLoginOTP'
    data = {"mobileNumber": mobile_number}
    response = session.post(url, json=data, headers=build_headers_shein(), timeout=TIMEOUT_SHEIN)
    response.raise_for_status()
    return response.json()

def login_with_otp(session: requests.Session, username: str, otp: str) -> requests.Response:
    url = 'https://www.sheinindia.in/api/auth/login'
    data = {"username": username, "otp": otp}
    response = session.post(url, json=data, headers=build_headers_shein(), timeout=TIMEOUT_SHEIN)
    response.raise_for_status()
    return response

def create_cart(session: requests.Session) -> str:
    resp = session.post("https://www.sheinindia.in/api/cart/create",
                        json={"user": "", "accessToken": ""}, headers=build_headers_shein(), timeout=TIMEOUT_SHEIN)
    resp.raise_for_status()
    set_cookie = resp.headers.get("Set-Cookie", "")
    match = re.search(r"C=([^;]+)", set_cookie)
    if not match:
        if 'C' in session.cookies:
            return session.cookies['C']
        raise RuntimeError("No cart code found.")
    return match.group(1)

def fetch_variants(session: requests.Session, product_id: str) -> List[Dict[str, Any]]:
    url = f"https://www.sheinindia.in/api/cart/sizeVariants/{product_id}"
    resp = session.get(url, headers=build_headers_shein(), timeout=TIMEOUT_SHEIN)
    resp.raise_for_status()
    data = resp.json()
    variants = []
    for base in data.get("baseOptions", []):
        if base.get("variantType") == "FnlSizeVariant":
            for opt in base.get("options", []):
                code = opt.get("code")
                stock = opt.get("stock", {}).get("stockLevel", 0)
                size = None
                for q in opt.get("variantOptionQualifiers", []):
                    if q.get("qualifier") == "size":
                        size = q.get("value")
                variants.append({"code": code, "size": size, "stock": stock})
    return variants

def choose_variant(variants: List[Dict[str, Any]], allowed_sizes: List[str]) -> Optional[Dict[str, Any]]:
    """Choose a variant with priority for allowed sizes, fallback to any in stock."""
    allowed_upper = {str(s).strip().upper() for s in allowed_sizes}
    
    # First priority: exact match with allowed sizes
    for v in variants:
        size_value = str(v.get("size", "")).strip().upper() if v.get("size") else ""
        stock = v.get("stock", 0)
        if size_value in allowed_upper and stock > 0:
            return v
    
    # Second priority: any available size in stock
    for v in variants:
        stock = v.get("stock", 0)
        if stock > 0:
            return v
    
    return None

def add_to_cart(session: requests.Session, cart_code: str, selected_code: str, quantity: int) -> Dict[str, Any]:
    url = f"https://www.sheinindia.in/api/cart/{cart_code}/product/{selected_code}/add"
    data = {"quantity": int(quantity)}
    resp = session.post(url, json=data, headers=build_headers_shein(), timeout=TIMEOUT_SHEIN)
    resp.raise_for_status()
    return resp.json()

def format_cookie_string(cookie_string: str) -> str:
    if not cookie_string:
        return ""
    cookies = {}
    for item in cookie_string.split(';'):
        if '=' in item:
            name, value = item.strip().split('=', 1)
            cookies[name] = value
    ordered_cookies = []
    for name in DESIRED_ORDER:
        if name in cookies:
            ordered_cookies.append(f"{name}={cookies[name]}")
            del cookies[name]
    remaining_names = sorted(cookies.keys())
    for name in remaining_names:
        ordered_cookies.append(f"{name}={cookies[name]}")
    return "; ".join(ordered_cookies)

# ---------------------------
# Voucher Validation Utilities (Async)
# ---------------------------
def get_voucher_value(voucher: str) -> int:
    """Get voucher value from prefix."""
    values = {"SVW": 4000, "SV6": 1000, "SVE": 2000, "SVF": 500}
    for prefix, value in values.items():
        if voucher.startswith(prefix):
            return value
    return 0

def create_progress_bar(current: int, total: int, bar_length: int = 20) -> str:
    """Create a progress bar like tqdm."""
    filled = int(bar_length * current / total)
    bar = "█" * filled + "░" * (bar_length - filled)
    percentage = int((current / total) * 100)
    return f"[{bar}] {percentage}% ({current}/{total})"

def create_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Create main menu keyboard."""
    initialize_user_data(user_id)
    config = user_data[user_id]
    cookie_status = "✅ Set" if config.get('cookie') else "❌ Not Set"
    
    # Check if any protections are active
    protection_count = len(protect_vouchers.get(user_id, []))
    protect_status_text = f"🛡️ ({protection_count})" if protection_count > 0 else ""
    
    keyboard = [
        [KeyboardButton("🍪 Login & Set Cookie"), KeyboardButton("⚙️ Settings")],
        [KeyboardButton(f"📊 Status: {cookie_status}"), KeyboardButton("🎟️ Validate Vouchers")],
        [KeyboardButton(f"🛡️ Protect Vouchers {protect_status_text}"), KeyboardButton("📋 Protection Status")],
        [KeyboardButton("❓ Help"), KeyboardButton("🔙 Back to Menu")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def create_protect_keyboard() -> InlineKeyboardMarkup:
    """Create inline keyboard for protection options."""
    keyboard = [
        [
            InlineKeyboardButton("➕ Add Vouchers", callback_data="protect_add"),
            InlineKeyboardButton("⏸️ Stop All", callback_data="protect_stop_all")
        ],
        [
            InlineKeyboardButton("📋 View Status", callback_data="protect_status"),
            InlineKeyboardButton("❌ Remove One", callback_data="protect_remove")
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data="protect_back")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_headers_voucher(cookie: str) -> Dict[str, str]:
    """Build headers for voucher API calls."""
    return {
        "Host": "www.sheinindia.in",
        "Accept": "application/json",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": "https://www.sheinindia.in",
        "Priority": "u=1, i",
        "Referer": "https://www.sheinindia.in/cart",
        "Sec-Ch-Ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": USER_AGENT,
        "X-Tenant-Id": "SHEIN",
        "Cookie": cookie,
    }

def create_aio_session(cookie: str) -> aiohttp.ClientSession:
    """Create aiohttp session."""
    connector = aiohttp.TCPConnector(limit=CONCURRENCY_LIMIT, limit_per_host=CONCURRENCY_LIMIT)
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_VOUCHER)
    session = aiohttp.ClientSession(headers=build_headers_voucher(cookie), connector=connector, timeout=timeout)
    return session

async def reset_voucher(session: aiohttp.ClientSession, voucher_id: str) -> bool:
    """Reset voucher asynchronously."""
    try:
        async with session.post(
            "https://www.sheinindia.in/api/cart/reset-voucher",
            json={"voucherId": voucher_id}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("voucherAmount", {}).get("value", 0) == 0
    except Exception as e:
        logging.error(f"Error resetting voucher {voucher_id}: {e}")
    return False

async def apply_voucher(session: aiohttp.ClientSession, voucher: str) -> Tuple[bool, int]:
    """Apply voucher asynchronously and return (success, amount)."""
    try:
        async with session.post(
            "https://www.sheinindia.in/api/cart/apply-voucher",
            json={"voucherId": voucher, "device": {"client_type": "MSITE"}}
        ) as resp:
            if resp.status == 200:
                resp_data = await resp.json()
                voucher_amount = resp_data.get("voucherAmount", {}).get("value", 0)
                applied_vouchers = resp_data.get("appliedVouchers", [])
                for applied in applied_vouchers:
                    if applied.get("voucherCode") == voucher or applied.get("code") == voucher:
                        return True, voucher_amount
                if voucher_amount > 0:
                    return True, voucher_amount
            else:
                logging.warning(f"Failed to apply voucher {voucher}: HTTP {resp.status}")
    except Exception as e:
        logging.error(f"Error applying voucher {voucher}: {e}")
    return False, 0

async def test_single_voucher(voucher: str, session: aiohttp.ClientSession) -> Tuple[str, int]:
    """Test a single voucher asynchronously."""
    try:
        success, amount = await apply_voucher(session, voucher)
        if success:
            await reset_voucher(session, voucher)
            value = get_voucher_value(voucher)
            logging.info(f"Valid voucher found: {voucher} (Value: ₹{value})")
            return voucher, value
    except Exception as e:
        logging.error(f"Error testing voucher {voucher}: {e}")
    return voucher, 0

async def test_vouchers(vouchers: List[str], progress_msg, cookie: str) -> List[Tuple[str, int]]:
    """Test vouchers concurrently with batches for progress updates."""
    session = create_aio_session(cookie)
    valid_vouchers = []
    total = len(vouchers)
    batch_size = CONCURRENCY_LIMIT
    try:
        for i in range(0, total, batch_size):
            batch = vouchers[i:i + batch_size]
            tasks = [test_single_voucher(v, session) for v in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in batch_results:
                if isinstance(result, tuple) and result[1] > 0:
                    valid_vouchers.append(result)
                elif isinstance(result, Exception):
                    logging.error(f"Batch error: {result}")
            current = min(i + batch_size, total)
            progress_bar = create_progress_bar(current, total)
            thunder = "⚡" if current % (batch_size * 2) == 0 else "🔥"
            try:
                await progress_msg.edit_text(
                    f"{thunder} Validating...\n{progress_bar}\n"
                    f"✅ Valid so far: {len(valid_vouchers)}"
                )
            except Exception as e:
                logging.error(f"Error updating progress: {e}")
            await asyncio.sleep(0.2)
    finally:
        await session.close()
    return valid_vouchers

# ---------------------------
# Conversation Handlers (Login Flow)
# ---------------------------
async def start_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for login conversation."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    config = user_data[user_id]
    await update.message.reply_text(
        f"Hello! To begin, please send me your 10-digit mobile number.\n\n"
        f"<b>Current Settings:</b>\n"
        f"Product ID: <code>{config['product_id']}</code>\n"
        f"Quantity: <code>{config['quantity']}</code>\n"
        f"Sizes: <code>{', '.join(config['sizes'])}</code>\n\n"
        f"You can change these using /set_product, /set_quantity, /set_sizes.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )
    return PHONE

async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle phone input."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    phone = update.message.text.strip()
    if not phone.isdigit() or len(phone) != 10:
        await update.message.reply_text("Invalid phone number. Please enter a valid 10-digit number.")
        return PHONE
    session = requests.Session()
    try:
        await update.message.reply_text("Checking account...")
        check_account(session, phone)
        await update.message.reply_text("Requesting OTP...")
        generate_otp(session, phone)
        await update.message.reply_text("✅ OTP sent! Please enter the 4-digit code you received.")
        context.user_data['session'] = session
        context.user_data['phone'] = phone
        return OTP
    except requests.exceptions.RequestException as e:
        await update.message.reply_text(f"❌ API Error: {e}")
        session.close()
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Unexpected error: {e}")
        session.close()
        return ConversationHandler.END

async def otp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle OTP and complete login/cart setup."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    otp = update.message.text.strip()
    if not otp.isdigit() or len(otp) != 4:
        await update.message.reply_text("Invalid OTP. Please enter a 4-digit code.")
        return OTP
    session: requests.Session = context.user_data['session']
    phone = context.user_data['phone']
    config = user_data[user_id]
    try:
        await update.message.reply_text("Logging in...")
        login_with_otp(session, phone, otp)
        await update.message.reply_text("✅ Login successful!")
        await update.message.reply_text("Creating cart and adding product...")
        cart_code = create_cart(session)
        variants = fetch_variants(session, config['product_id'])
        if not variants:
            await update.message.reply_text("❌ No variants available for this product.")
            session.close()
            return ConversationHandler.END
        
        selected = choose_variant(variants, config['sizes'])
        if not selected:
            await update.message.reply_text(
                f"❌ No sizes in stock for this product.\n"
                f"Product ID: {config['product_id']}\n"
                f"Please try a different product using /set_product <new_product_id>"
            )
            session.close()
            return ConversationHandler.END
        
        # Inform user which size was selected
        selected_size = selected.get("size", "Unknown")
        preferred_sizes = {str(s).strip().upper() for s in config['sizes']}
        selected_size_upper = str(selected_size).strip().upper() if selected_size else ""
        
        if selected_size_upper in preferred_sizes:
            size_message = f"✅ Selected your preferred size: {selected_size}"
        else:
            size_message = f"⚠️ Your preferred sizes not available. Using available size: {selected_size}"
        
        await update.message.reply_text(size_message)
        
        add_to_cart(session, cart_code, selected["code"], config['quantity'])
        await update.message.reply_text("✅ Product added to cart!")
        raw_cookie_string = "; ".join([f"{k}={v}" for k, v in session.cookies.get_dict().items()])
        formatted_cookie_string = format_cookie_string(raw_cookie_string)
        user_data[user_id]['cookie'] = formatted_cookie_string
        save_cookie_to_db(user_id, formatted_cookie_string, phone)
        await update.message.reply_text(
            "✅ Cookie set successfully! You can now validate and protect vouchers.",
            reply_markup=create_main_keyboard(user_id)
        )
        session.close()
        context.user_data.pop('session', None)
        context.user_data.pop('phone', None)
        return ConversationHandler.END
    except requests.exceptions.RequestException as e:
        await update.message.reply_text(f"❌ API Error: {e}")
        session.close()
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Unexpected error: {e}")
        session.close()
        return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel login conversation."""
    user_id = update.effective_user.id
    if 'session' in context.user_data:
        context.user_data['session'].close()
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Operation cancelled.\n\nReturning to main menu:",
        reply_markup=create_main_keyboard(user_id)
    )
    return ConversationHandler.END

# ---------------------------
# Settings Commands
# ---------------------------
async def set_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set product ID."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    if not context.args:
        await update.message.reply_text("Usage: /set_product <product_id>")
        return
    product_id = ' '.join(context.args)
    user_data[user_id]['product_id'] = product_id
    await update.message.reply_text(f"✅ Product ID set to: <code>{product_id}</code>", parse_mode="HTML")

async def set_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set quantity."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    if not context.args:
        await update.message.reply_text("Usage: /set_quantity <quantity>")
        return
    try:
        quantity = int(context.args[0])
        user_data[user_id]['quantity'] = quantity
        await update.message.reply_text(f"✅ Quantity set to: <code>{quantity}</code>", parse_mode="HTML")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_quantity <quantity>")

async def set_sizes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set sizes."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    if not context.args:
        await update.message.reply_text("Usage: /set_sizes <size1> <size2> ...")
        return
    sizes = ' '.join(context.args).split()
    user_data[user_id]['sizes'] = sizes
    await update.message.reply_text(f"✅ Sizes set to: <code>{', '.join(sizes)}</code>", parse_mode="HTML")

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current settings."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    config = user_data[user_id]
    cookie_status = "✅ Set" if config.get('cookie') else "❌ Not Set"
    
    # Protection status
    protection_count = len(protect_vouchers.get(user_id, []))
    protect_info = f"Active protections: {protection_count}" if protection_count > 0 else "No active protections"
    
    text = (
        f"<b>Current Settings:</b>\n\n"
        f"🍪 Cookie: {cookie_status}\n"
        f"🆔 Product ID: <code>{config['product_id']}</code>\n"
        f"📦 Quantity: <code>{config['quantity']}</code>\n"
        f"👕 Sizes: <code>{', '.join(config['sizes'])}</code>\n"
        f"🛡️ Protection: {protect_info}\n\n"
        f"<i>Note: If preferred sizes are unavailable, any available size will be used.</i>\n\n"
        f"Use /set_ commands to change."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=create_main_keyboard(user_id))

# ---------------------------
# Voucher Protection Handlers
# ---------------------------
async def protect_vouchers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle protect vouchers command."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    config = user_data[user_id]
    
    if not config.get('cookie'):
        await update.message.reply_text(
            "❌ No cookie set. Please login first.\n\nUse 🔙 Back to Menu.",
            reply_markup=create_main_keyboard(user_id)
        )
        return
    
    await update.message.reply_text(
        "🛡️ *Voucher Protection System*\n\n"
        "*What it does:*\n"
        "• Automatically applies vouchers every 10 minutes\n"
        "• Prevents others from using your vouchers\n"
        "• Runs in background until stopped\n\n"
        "*How to use:*\n"
        "1. Add vouchers to protect\n"
        "2. System will auto-apply every 10 min\n"
        "3. Stop when you want to use them\n\n"
        "Choose an option:",
        parse_mode="Markdown",
        reply_markup=create_protect_keyboard()
    )

async def protect_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle protect callback queries."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    if data == "protect_add":
        await query.edit_message_text(
            "➕ *Add Vouchers to Protect*\n\n"
            "Send vouchers (one per line):\n"
            "```\nSVW123456\nSVE789012\nSVF345678\n```\n\n"
            "These will be automatically applied every 10 minutes.\n"
            "Use /cancel to go back.",
            parse_mode="Markdown"
        )
        context.user_data['expecting_protect_vouchers'] = True
        
    elif data == "protect_stop_all":
        if user_id in protect_vouchers and protect_vouchers[user_id]:
            await stop_voucher_protection(user_id)
            await query.edit_message_text(
                "⏸️ *All Protections Stopped*\n\n"
                "All vouchers have been removed from protection.\n"
                "You can now use them normally.",
                parse_mode="Markdown",
                reply_markup=create_protect_keyboard()
            )
        else:
            await query.edit_message_text(
                "❌ No active protections found.",
                parse_mode="Markdown",
                reply_markup=create_protect_keyboard()
            )
            
    elif data == "protect_status":
        status_text = get_protection_status(user_id)
        await query.edit_message_text(
            f"📋 *Protection Status*\n\n{status_text}\n\n"
            "Last updated: " + datetime.now().strftime("%H:%M:%S"),
            parse_mode="Markdown",
            reply_markup=create_protect_keyboard()
        )
        
    elif data == "protect_remove":
        if user_id in protect_vouchers and protect_vouchers[user_id]:
            vouchers_list = "\n".join([f"`{v}`" for v in protect_vouchers[user_id]])
            await query.edit_message_text(
                f"❌ *Remove Voucher*\n\n"
                f"Current protected vouchers:\n{vouchers_list}\n\n"
                f"Send the voucher code you want to remove:",
                parse_mode="Markdown"
            )
            context.user_data['expecting_remove_voucher'] = True
        else:
            await query.edit_message_text(
                "❌ No active protections found.",
                parse_mode="Markdown",
                reply_markup=create_protect_keyboard()
            )
            
    elif data == "protect_back":
        await query.edit_message_text(
            "Returning to main menu...",
            parse_mode="Markdown"
        )
        await query.message.reply_text(
            "🏠 *Main Menu*\n\nChoose an option:",
            parse_mode="Markdown",
            reply_markup=create_main_keyboard(user_id)
        )

async def protection_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show protection status."""
    user_id = update.effective_user.id
    status_text = get_protection_status(user_id)
    
    if status_text == "❌ No active protections":
        await update.message.reply_text(
            status_text + "\n\nUse 🛡️ Protect Vouchers to start.",
            reply_markup=create_main_keyboard(user_id)
        )
    else:
        await update.message.reply_text(
            f"📋 *Protection Status*\n\n{status_text}\n\n"
            "Last updated: " + datetime.now().strftime("%H:%M:%S"),
            parse_mode="Markdown",
            reply_markup=create_main_keyboard(user_id)
        )

# ---------------------------
# Telegram Handlers
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start command."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    
    # Check for active protections
    protection_count = len(protect_vouchers.get(user_id, []))
    protect_text = f"\n\n🛡️ *Active Protections:* {protection_count}" if protection_count > 0 else ""
    
    await update.message.reply_text(
        "⚡ *Shein Bot: Login + Voucher Validator*\n\n"
        "Welcome! Use the keyboard to navigate.\n\n"
        "1. Login & Set Cookie (phone/OTP) to authenticate.\n"
        "2. Validate vouchers with your session.\n"
        "3. 🛡️ Protect vouchers from being used by others.\n"
        f"{protect_text}\n\n"
        "*Voucher Protection:*\n"
        "• Auto-applies every 10 minutes\n"
        "• Prevents others from using your vouchers\n"
        "• Runs in background\n\n"
        "Use menu below:",
        parse_mode="Markdown",
        reply_markup=create_main_keyboard(user_id)
    )

async def handle_keyboard_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle non-login keyboard buttons."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    text = update.message.text
    
    if text == "⚙️ Settings":
        await show_settings(update, context)
        
    elif text.startswith("📊 Status:"):
        status = text.split(": ", 1)[1]
        await update.message.reply_text(
            f"Your cookie is {status}.\nFor full settings, use ⚙️ Settings.",
            reply_markup=create_main_keyboard(user_id)
        )
        
    elif text == "🍪 Login & Set Cookie":
        return  # Handled by conversation handler
        
    elif text == "🎟️ Validate Vouchers":
        config = user_data[user_id]
        if not config.get('cookie'):
            await update.message.reply_text(
                "❌ No cookie set. Please login first.\n\nUse 🔙 Back to Menu.",
                reply_markup=create_main_keyboard(user_id)
            )
        else:
            await update.message.reply_text(
                "🎟️ *Validate Vouchers*\n\n"
                "Send your vouchers (one per line).\n"
                "Example:\n"
                "```\nSVW123456\nSVE789012\nSVF345678\n```\n\n"
                "Use /cancel to go back.",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove()
            )
            context.user_data['expecting_vouchers'] = True
            
    elif text.startswith("🛡️ Protect Vouchers"):
        await protect_vouchers_handler(update, context)
        
    elif text == "📋 Protection Status":
        await protection_status_handler(update, context)
        
    elif text == "❓ Help":
        await update.message.reply_text(
            "❓ *Help*\n\n"
            "💰 Voucher Values:\n"
            "• SVW: ₹4,000\n"
            "• SV6: ₹1,000\n"
            "• SVE: ₹2,000\n"
            "• SVF: ₹500\n\n"
            "📋 How to use:\n"
            "1. Use 🍪 Login & Set Cookie\n"
            "2. Validate or protect vouchers\n\n"
            "🛡️ *Voucher Protection:*\n"
            "• Automatically applies every 10 minutes\n"
            "• Prevents others from using your vouchers\n"
            "• Perfect for reserving vouchers\n"
            "• Runs until manually stopped\n\n"
            "⚡ Features:\n"
            "• Concurrent validation\n"
            "• Size flexibility\n"
            "• Background protection\n\n"
            "Use 🔙 Back to Menu.",
            parse_mode="Markdown",
            reply_markup=create_main_keyboard(user_id)
        )
        
    elif text == "🔙 Back to Menu":
        context.user_data.pop('expecting_vouchers', None)
        context.user_data.pop('expecting_protect_vouchers', None)
        context.user_data.pop('expecting_remove_voucher', None)
        await update.message.reply_text(
            "🏠 *Main Menu*\n\nChoose an option:",
            parse_mode="Markdown",
            reply_markup=create_main_keyboard(user_id)
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel current operation."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    context.user_data.pop('expecting_vouchers', None)
    context.user_data.pop('expecting_protect_vouchers', None)
    context.user_data.pop('expecting_remove_voucher', None)
    await update.message.reply_text(
        "❌ Cancelled.\n\nReturning to menu:",
        reply_markup=create_main_keyboard(user_id)
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle general text (vouchers or buttons)."""
    user_id = update.effective_user.id
    initialize_user_data(user_id)
    text = update.message.text

    # Handle protect vouchers addition
    if context.user_data.get('expecting_protect_vouchers'):
        context.user_data['expecting_protect_vouchers'] = False
        config = user_data[user_id]
        
        if not config.get('cookie'):
            await update.message.reply_text(
                "❌ No cookie set. Please login first.",
                reply_markup=create_main_keyboard(user_id)
            )
            return
            
        vouchers = [line.strip().upper() for line in text.split('\n') if line.strip()]
        if not vouchers:
            await update.message.reply_text("❌ No vouchers found.", reply_markup=create_main_keyboard(user_id))
            return
            
        # Start protection
        added = await start_voucher_protection(user_id, vouchers, context)
        
        if added:
            await update.message.reply_text(
                f"✅ *Protection Started*\n\n"
                f"Vouchers added: {len(vouchers)}\n"
                f"Will auto-apply every 10 minutes.\n\n"
                f"Use 📋 Protection Status to monitor.",
                parse_mode="Markdown",
                reply_markup=create_main_keyboard(user_id)
            )
        else:
            await update.message.reply_text(
                "❌ Failed to start protection.",
                reply_markup=create_main_keyboard(user_id)
            )
        return
    
    # Handle voucher removal
    elif context.user_data.get('expecting_remove_voucher'):
        context.user_data['expecting_remove_voucher'] = False
        voucher_to_remove = text.strip().upper()
        
        if user_id in protect_vouchers and voucher_to_remove in protect_vouchers[user_id]:
            await stop_voucher_protection(user_id, voucher_to_remove)
            await update.message.reply_text(
                f"✅ Removed `{voucher_to_remove}` from protection.",
                parse_mode="Markdown",
                reply_markup=create_main_keyboard(user_id)
            )
        else:
            await update.message.reply_text(
                f"❌ Voucher `{voucher_to_remove}` not found in protected list.",
                parse_mode="Markdown",
                reply_markup=create_main_keyboard(user_id)
            )
        return

    # Handle regular vouchers validation
    elif context.user_data.get('expecting_vouchers'):
        context.user_data['expecting_vouchers'] = False
        config = user_data[user_id]
        if not config.get('cookie'):
            await update.message.reply_text(
                "❌ No cookie set. Please login first.",
                reply_markup=create_main_keyboard(user_id)
            )
            return
        vouchers = [line.strip() for line in text.split('\n') if line.strip()]
        if not vouchers:
            await update.message.reply_text("❌ No vouchers found.", reply_markup=create_main_keyboard(user_id))
            return
        progress_msg = await update.message.reply_text("⚡ Starting validation...")
        try:
            valid_vouchers = await test_vouchers(vouchers, progress_msg, config['cookie'])
        except Exception as e:
            logging.error(f"Validation error: {e}")
            await progress_msg.edit_text(
                "❌ Error during validation. Try again.",
                reply_markup=create_main_keyboard(user_id)
            )
            return
        if not valid_vouchers:
            await progress_msg.edit_text(
                "❌ No valid vouchers found.\n\nUse menu.",
                reply_markup=create_main_keyboard(user_id)
            )
            return
        valid_vouchers.sort(key=lambda x: x[1], reverse=True)
        total_value = sum(v for _, v in valid_vouchers)
        result_lines = [f"• {v} - ₹{val}" for v, val in valid_vouchers]
        copy_text = "\n".join([v for v, _ in valid_vouchers])
        message = (
            f"✅ {len(valid_vouchers)} VALID | Total: ₹{total_value}\n\n"
            f"{'\n'.join(result_lines)}\n\n"
            f"📋 *Copy:*\n"
            f"```\n{copy_text}\n```\n\n"
            f"💡 *Tip:* Use 🛡️ Protect Vouchers to reserve these!"
        )
        try:
            await progress_msg.edit_text(message, parse_mode="Markdown")
        except Exception:
            fallback = (
                f"✅ {len(valid_vouchers)} VALID | Total: ₹{total_value}\n\n"
                + "\n".join([f"{v} - ₹{val}" for v, val in valid_vouchers])
            )
            await progress_msg.edit_text(fallback)
        await update.message.reply_text("Complete! Use menu:", reply_markup=create_main_keyboard(user_id))
        return

    # Handle buttons
    button_texts = [
        "🍪 Login & Set Cookie", "⚙️ Settings", "🎟️ Validate Vouchers",
        "🛡️ Protect Vouchers", "📋 Protection Status", "❓ Help", "🔙 Back to Menu"
    ]
    if any(text.startswith(btn) for btn in button_texts) or text.startswith("📊 Status:"):
        await handle_keyboard_buttons(update, context)
        return

    # Default: show menu
    await update.message.reply_text(
        "Use the keyboard to navigate:",
        reply_markup=create_main_keyboard(user_id)
    )

# ---------------------------
# Main
# ---------------------------
def main() -> None:
    """Run the bot."""
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler for login
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'^🍪 Login & Set Cookie$'), start_login)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_handler)],
            OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settings", show_settings))
    app.add_handler(CommandHandler("set_product", set_product))
    app.add_handler(CommandHandler("set_quantity", set_quantity))
    app.add_handler(CommandHandler("set_sizes", set_sizes))
    app.add_handler(CommandHandler("cancel", cancel))

    # Callback handler for protection
    app.add_handler(CallbackQueryHandler(protect_callback_handler, pattern=r'^protect_'))

    # Add conversation first
    app.add_handler(conv_handler)
    # General text handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logging.info("⚡ Shein Bot with Voucher Protection started!")
    app.run_polling()

if __name__ == "__main__":
    main()