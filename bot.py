import os
import sys
import time
import json
import sqlite3
import requests
import threading
import logging
import pyzipper
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlparse, unquote

# ================= تنظیمات =================
TOKEN = os.environ.get("BALE_BOT_TOKEN")
if not TOKEN:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        TOKEN = os.environ.get("BALE_BOT_TOKEN")
    except ImportError:
        pass
    if not TOKEN:
        raise ValueError("❌ BALE_BOT_TOKEN environment variable is not set!")

ALLOWED_USER_ID = 1193977634
ADMIN_USERNAME = "@mindscoder"
BOT_USERNAME = "@pedarattubebot"

BASE_URL = f"https://tapi.bale.ai/bot{TOKEN}"
DB_FILE = "uploader_bot.db"
DOWNLOAD_DIR = "downloads"
TEMP_DIR = "temp"
MAX_FILE_SIZE = 19 * 1024 * 1024
UPLOAD_RETRY_COUNT = 5
UPLOAD_RETRY_DELAY = 3

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
# conf
# ================= دیتابیس =================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            filename TEXT,
            file_size INTEGER,
            download_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT,
            file_size INTEGER,
            parts_count INTEGER,
            upload_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_downloaded INTEGER DEFAULT 0,
            total_uploaded INTEGER DEFAULT 0,
            total_download_size INTEGER DEFAULT 0,
            total_upload_size INTEGER DEFAULT 0
        )
    ''')
    c.execute('SELECT COUNT(*) FROM stats')
    if c.fetchone()[0] == 0:
        c.execute('INSERT INTO stats (total_downloaded, total_uploaded, total_download_size, total_upload_size) VALUES (0, 0, 0, 0)')
    conn.commit()
    conn.close()

def update_stats(download_size: int = 0, upload_size: int = 0, is_download: bool = True):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if is_download:
        c.execute('UPDATE stats SET total_downloaded = total_downloaded + 1, total_download_size = total_download_size + ?', (download_size,))
    else:
        c.execute('UPDATE stats SET total_uploaded = total_uploaded + 1, total_upload_size = total_upload_size + ?', (upload_size,))
    conn.commit()
    conn.close()

def add_download_record(url: str, filename: str, file_size: int, status: str = "completed"):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO downloads (url, filename, file_size, status) VALUES (?, ?, ?, ?)', (url, filename, file_size, status))
    conn.commit()
    conn.close()

def add_upload_record(file_path: str, file_size: int, parts_count: int, status: str = "completed"):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('INSERT INTO uploads (file_path, file_size, parts_count, status) VALUES (?, ?, ?, ?)', (file_path, file_size, parts_count, status))
    conn.commit()
    conn.close()

def get_stats() -> Dict:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM stats ORDER BY id DESC LIMIT 1')
    row = c.fetchone()
    conn.close()
    if row:
        return {'total_downloaded': row[1], 'total_uploaded': row[2], 'total_download_size': row[3], 'total_upload_size': row[4]}
    return {'total_downloaded': 0, 'total_uploaded': 0, 'total_download_size': 0, 'total_upload_size': 0}

# ================= توابع کمکی =================
def format_size(size_bytes: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"

def format_time(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} ثانیه"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes} دقیقه و {secs} ثانیه"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours} ساعت و {minutes} دقیقه"

def create_progress_bar(percentage: float, width: int = 20) -> str:
    filled = int(width * percentage / 100)
    empty = width - filled
    return "█" * filled + "░" * empty + f" {percentage:.1f}%"

def is_valid_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except:
        return False

def get_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if not filename or '.' not in filename:
        try:
            response = requests.head(url, timeout=10, allow_redirects=True)
            if 'Content-Disposition' in response.headers:
                import re
                cd = response.headers['Content-Disposition']
                filenames = re.findall('filename="?([^";]+)"?', cd)
                if filenames:
                    filename = filenames[0]
        except:
            pass
    if not filename or '.' not in filename:
        filename = f"file_{int(time.time())}.bin"
    return filename

def save_webpage_as_html(url: str, filepath: str) -> bool:
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(response.text)
        return True
    except Exception as e:
        logger.error(f"Error saving webpage: {e}")
        return False

# ================= کلاس‌های پیشرفت =================
class DownloadProgress:
    def __init__(self, chat_id: int, msg_id: int, total_size: int, filename: str):
        self.chat_id = chat_id
        self.msg_id = msg_id
        self.total_size = total_size
        self.filename = filename
        self.downloaded = 0
        self.start_time = time.time()
        self.last_update = 0
        self.speed_history = []

    def update(self, chunk_size: int):
        self.downloaded += chunk_size
        current_time = time.time()
        if current_time - self.last_update >= 2:
            self.last_update = current_time
            elapsed = current_time - self.start_time
            percentage = (self.downloaded / self.total_size) * 100
            speed = self.downloaded / elapsed if elapsed > 0 else 0
            self.speed_history.append(speed)
            if len(self.speed_history) > 5:
                self.speed_history.pop(0)
            avg_speed = sum(self.speed_history) / len(self.speed_history) if self.speed_history else speed
            remaining_bytes = self.total_size - self.downloaded
            eta = remaining_bytes / avg_speed if avg_speed > 0 else 0
            progress_bar = create_progress_bar(percentage)
            message = f"""📥 **در حال دانلود**
📁 {self.filename}

{progress_bar}

💾 حجم: {format_size(self.downloaded)} / {format_size(self.total_size)}
⚡ سرعت: {format_size(avg_speed)}/s
⏱ زمان باقی‌مانده: {format_time(eta)}
⏳ زمان سپری شده: {format_time(elapsed)}"""
            try:
                edit_message_text(self.chat_id, self.msg_id, message)
            except:
                pass

class UploadProgress:
    def __init__(self, chat_id: int, total_parts: int):
        self.chat_id = chat_id
        self.total_parts = total_parts
        self.current_part = 0
        self.start_time = time.time()
        self.last_update = 0

    def update(self):
        self.current_part += 1
        current_time = time.time()
        if current_time - self.last_update >= 1 or self.current_part == self.total_parts:
            self.last_update = current_time
            elapsed = current_time - self.start_time
            percentage = (self.current_part / self.total_parts) * 100
            if self.current_part > 0:
                time_per_part = elapsed / self.current_part
                remaining_parts = self.total_parts - self.current_part
                eta = remaining_parts * time_per_part
            else:
                eta = 0
            progress_bar = create_progress_bar(percentage)
            message = f"""📤 **در حال آپلود**
{progress_bar}

📦 قطعه: {self.current_part} از {self.total_parts}
⏱ زمان باقی‌مانده: {format_time(eta)}
⏳ زمان سپری شده: {format_time(elapsed)}"""
            return message
        return None

# ================= توابع دانلود و آپلود =================
def download_file_with_progress(url: str, chat_id: int, status_msg_id: int) -> Optional[str]:
    try:
        response = requests.get(url, stream=True, timeout=30)
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))
        filename = get_filename_from_url(url)
        if total_size == 0:
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            with open(filepath, 'wb') as f:
                f.write(response.content)
            return filepath
        filepath = os.path.join(DOWNLOAD_DIR, filename)
        progress = DownloadProgress(chat_id, status_msg_id, total_size, filename)
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    progress.update(len(chunk))
        return filepath
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None

def split_file_to_zip_parts(file_path: str, max_size: int = MAX_FILE_SIZE) -> Tuple[List[str], int]:
    """
    فایل را به قطعات ZIP استاندارد تقسیم می‌کند.
    از pyzipper.ZipFile استفاده می‌کند که نیازی به رمز عبور ندارد.
    """
    base_name = os.path.basename(file_path)
    zip_file_path = os.path.join(TEMP_DIR, f"{base_name}.zip")

    # 1. ساخت فایل ZIP استاندارد با pyzipper.ZipFile (بدون رمز)
    try:
        with pyzipper.ZipFile(zip_file_path, 'w', compression=pyzipper.ZIP_DEFLATED) as zf:
            zf.write(file_path, base_name)
        logger.info(f"Standard ZIP created at {zip_file_path}")
    except Exception as e:
        logger.error(f"Failed to create ZIP file: {e}")
        return [], 0

    # 2. تقسیم فایل ZIP به قطعات کوچکتر
    part_paths = []
    part_num = 1
    try:
        with open(zip_file_path, 'rb') as f:
            while True:
                chunk = f.read(max_size)
                if not chunk:
                    break
                part_filename = f"{file_path}.zip.{part_num:03d}"
                with open(part_filename, 'wb') as part_file:
                    part_file.write(chunk)
                part_paths.append(part_filename)
                part_num += 1
    except Exception as e:
        logger.error(f"Failed to split ZIP file: {e}")
        for p in part_paths:
            if os.path.exists(p):
                os.remove(p)
        return [], 0
    finally:
        # حذف فایل ZIP موقت
        if os.path.exists(zip_file_path):
            os.remove(zip_file_path)

    return part_paths, part_num - 1

def upload_part_with_retry(chat_id: int, part_path: str, caption: str, max_retries: int = UPLOAD_RETRY_COUNT) -> bool:
    """آپلود یک قطعه با تلاش مجدد در صورت خطا یا تایم‌اوت"""
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Uploading {os.path.basename(part_path)} (attempt {attempt}/{max_retries})")
            result = send_document(chat_id, part_path, caption)
            if result.get('ok'):
                return True
            else:
                logger.warning(f"Upload failed: {result.get('description', 'Unknown error')}")
        except Exception as e:
            logger.error(f"Upload exception: {e}")
        if attempt < max_retries:
            time.sleep(UPLOAD_RETRY_DELAY)
    return False

# ================= ارتباط با بله =================
def api_call(method: str, payload: dict = None, files: dict = None) -> dict:
    try:
        url = f"{BASE_URL}/{method}"
        if files:
            res = requests.post(url, data=payload, files=files, timeout=120)
        else:
            res = requests.post(url, json=payload or {}, timeout=30)
        return res.json() if res.status_code == 200 else {}
    except Exception as e:
        logger.error(f"API Exception in {method}: {e}")
        return {}

def send_message(chat_id: int, text: str, reply_to: int = None, reply_markup: dict = None) -> Optional[int]:
    footer = f"\n\n{BOT_USERNAME}"
    payload = {"chat_id": chat_id, "text": text + footer}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    result = api_call("sendMessage", payload)
    if result.get('ok') and 'result' in result:
        return result['result'].get('message_id')
    return None

def edit_message_text(chat_id: int, message_id: int, text: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    return api_call("editMessageText", payload)

def send_document(chat_id: int, file_path: str, caption: str = None, reply_to: int = None):
    footer = f"\n\n{BOT_USERNAME}"
    full_caption = (caption or "") + footer
    with open(file_path, 'rb') as f:
        files = {"document": f}
        data = {"chat_id": chat_id}
        if full_caption:
            data["caption"] = full_caption
        if reply_to:
            data["reply_to_message_id"] = reply_to
        return api_call("sendDocument", payload=data, files=files)

def get_updates(offset: int = None) -> dict:
    payload = {"timeout": 30}
    if offset:
        payload["offset"] = offset
    return api_call("getUpdates", payload)

# ================= کیبورد =================
def admin_panel_keyboard():
    return {
        "keyboard": [
            [{"text": "📥 دانلود از لینک"}, {"text": "🌐 ذخیره صفحه وب"}],
            [{"text": "📊 آمار ربات"}, {"text": "🗑️ پاکسازی فایل‌ها"}],
            [{"text": "ℹ️ راهنما"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

# ================= پردازش اصلی =================
user_states = {}

def handle_message(update: dict):
    try:
        if "message" not in update:
            return
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = msg.get("text", "").strip()
        msg_id = msg.get("message_id")

        logger.info(f"Message from {user_id}: {text[:50] if text else '[NO TEXT]'}")

        # کنترل دسترسی
        if user_id != ALLOWED_USER_ID:
            send_message(
                chat_id,
                f"❌️ بیلاخ داداش ادمین نیستی، ادمین بات: {ADMIN_USERNAME}",
                reply_to=msg_id
            )
            return

        # مدیریت حالت‌ها
        if user_id in user_states:
            state = user_states[user_id]
            if state == "waiting_for_url":
                if is_valid_url(text):
                    del user_states[user_id]
                    process_download_url(chat_id, msg_id, text)
                else:
                    send_message(chat_id, "❌ لینک نامعتبر است. لطفاً یک URL معتبر وارد کنید.", reply_to=msg_id)
                return
            elif state == "waiting_for_webpage":
                if is_valid_url(text):
                    del user_states[user_id]
                    process_webpage_save(chat_id, msg_id, text)
                else:
                    send_message(chat_id, "❌ لینک نامعتبر است.", reply_to=msg_id)
                return

        # دکمه‌های منو
        if text == "📥 دانلود از لینک":
            user_states[user_id] = "waiting_for_url"
            send_message(chat_id, "🔗 لطفاً لینک فایل مورد نظر را ارسال کنید:", reply_to=msg_id)
        elif text == "🌐 ذخیره صفحه وب":
            user_states[user_id] = "waiting_for_webpage"
            send_message(chat_id, "🌐 لطفاً آدرس صفحه وب را ارسال کنید (به صورت HTML ذخیره می‌شود):", reply_to=msg_id)
        elif text == "📊 آمار ربات":
            stats = get_stats()
            message = f"""📊 **آمار ربات**
📥 تعداد دانلودها: {stats['total_downloaded']}
💾 حجم کل دانلود: {format_size(stats['total_download_size'])}
📤 تعداد آپلودها: {stats['total_uploaded']}
💾 حجم کل آپلود: {format_size(stats['total_upload_size'])}"""
            send_message(chat_id, message, reply_to=msg_id)
        elif text == "🗑️ پاکسازی فایل‌ها":
            cleanup_files()
            send_message(chat_id, "✅ فایل‌های موقت و دانلود شده پاکسازی شدند.", reply_to=msg_id)
        elif text == "ℹ️ راهنما":
            help_text = f"""📚 **راهنمای ربات آپلودر**
• 📥 دانلود از لینک: فایل از URL دانلود و آپلود می‌شود
• 🌐 ذخیره صفحه وب: صفحه وب به صورت HTML ذخیره می‌شود
• فایل‌های زیر ۱۹ مگ مستقیم آپلود می‌شوند
• فایل‌های بالای ۱۹ مگ به قطعات ZIP تقسیم می‌شوند
👨‍💻 ادمین: {ADMIN_USERNAME}"""
            send_message(chat_id, help_text, reply_to=msg_id)
        elif text in ["/start", "/panel"]:
            send_message(chat_id, "🎛 **پنل مدیریت آپلودر**\nلطفاً یک گزینه را انتخاب کنید:",
                         reply_markup=admin_panel_keyboard(), reply_to=msg_id)
        else:
            send_message(chat_id, "لطفاً از دکمه‌های منو استفاده کنید یا /panel را بزنید.",
                         reply_markup=admin_panel_keyboard())
    except Exception as e:
        logger.error(f"Handle message error: {e}")

def process_download_url(chat_id: int, reply_to: int, url: str):
    status_msg = send_message(chat_id, "⏳ در حال دریافت اطلاعات فایل...", reply_to=reply_to)
    if not status_msg:
        return
    try:
        # اطلاعات اولیه
        response = requests.head(url, timeout=30, allow_redirects=True)
        total_size = int(response.headers.get('content-length', 0))
        filename = get_filename_from_url(url)

        if total_size > 0:
            edit_message_text(chat_id, status_msg, f"📁 فایل: {filename}\n💾 حجم: {format_size(total_size)}\n⏳ شروع دانلود...")
        else:
            edit_message_text(chat_id, status_msg, f"📁 فایل: {filename}\n💾 حجم: نامشخص\n⏳ شروع دانلود...")

        start_time = time.time()
        file_path = download_file_with_progress(url, chat_id, status_msg)
        download_time = time.time() - start_time

        if not file_path:
            edit_message_text(chat_id, status_msg, "❌ خطا در دانلود فایل")
            return

        file_size = os.path.getsize(file_path)
        update_stats(download_size=file_size, is_download=True)
        add_download_record(url, filename, file_size)

        edit_message_text(chat_id, status_msg, f"✅ دانلود کامل شد!\n⏱ زمان دانلود: {format_time(download_time)}\n📤 در حال آپلود...")

        if file_size <= MAX_FILE_SIZE:
            # آپلود مستقیم
            result = send_document(chat_id, file_path, f"✅ فایل با موفقیت آپلود شد\n📁 {filename}")
            if result.get('ok'):
                edit_message_text(chat_id, status_msg, f"✅ عملیات با موفقیت انجام شد!\n📁 {filename}\n💾 {format_size(file_size)}")
                update_stats(upload_size=file_size, is_download=False)
                add_upload_record(file_path, file_size, 1)
                os.remove(file_path)
            else:
                edit_message_text(chat_id, status_msg, "❌ آپلود فایل با خطا مواجه شد.")
        else:
            # تقسیم و آپلود قطعات با روش جدید (pyzipper.ZipFile)
            edit_message_text(chat_id, status_msg, "📦 در حال فشرده‌سازی و تقسیم فایل به قطعات ۱۹ مگابایتی...")
            parts, parts_count = split_file_to_zip_parts(file_path)

            if parts_count == 0:
                edit_message_text(chat_id, status_msg, "❌ خطا در تقسیم فایل.")
                os.remove(file_path)
                return

            upload_progress = UploadProgress(chat_id, parts_count)

            successful_parts = 0
            for i, part_path in enumerate(parts, 1):
                # نمایش پیشرفت
                progress_msg = upload_progress.update()
                if progress_msg:
                    edit_message_text(chat_id, status_msg, progress_msg)

                caption = f"📦 قطعه {i} از {parts_count}\n📁 {filename}"
                # تلاش تا موفقیت
                success = upload_part_with_retry(chat_id, part_path, caption)
                if not success:
                    edit_message_text(chat_id, status_msg, f"❌ آپلود قطعه {i} پس از {UPLOAD_RETRY_COUNT} تلاش ناموفق ماند. عملیات متوقف شد.")
                    # پاکسازی قطعات باقی‌مانده
                    for p in parts:
                        if os.path.exists(p):
                            os.remove(p)
                    os.remove(file_path)
                    return
                successful_parts += 1
                time.sleep(1)

            edit_message_text(chat_id, status_msg,
                f"✅ فایل با موفقیت به {parts_count} قطعه تقسیم و آپلود شد!\n📁 {filename}\n💾 {format_size(file_size)}")
            update_stats(upload_size=file_size, is_download=False)
            add_upload_record(file_path, file_size, parts_count)

            # پاکسازی
            os.remove(file_path)
            for part in parts:
                if os.path.exists(part):
                    os.remove(part)
    except Exception as e:
        logger.error(f"Process download error: {e}")
        try:
            edit_message_text(chat_id, status_msg, f"❌ خطا در پردازش: {str(e)[:100]}")
        except:
            pass

def process_webpage_save(chat_id: int, reply_to: int, url: str):
    status_msg = send_message(chat_id, "⏳ در حال دریافت صفحه وب...", reply_to=reply_to)
    if not status_msg:
        return
    try:
        filename = f"webpage_{int(time.time())}.html"
        file_path = os.path.join(DOWNLOAD_DIR, filename)
        if save_webpage_as_html(url, file_path):
            file_size = os.path.getsize(file_path)
            update_stats(download_size=file_size, is_download=True)
            add_download_record(url, filename, file_size)

            result = send_document(chat_id, file_path, f"✅ صفحه وب با موفقیت ذخیره شد\n🌐 {url}\n💾 {format_size(file_size)}")
            if result.get('ok'):
                edit_message_text(chat_id, status_msg, f"✅ عملیات با موفقیت انجام شد!\n📁 {filename}\n💾 {format_size(file_size)}")
                update_stats(upload_size=file_size, is_download=False)
                add_upload_record(file_path, file_size, 1)
                os.remove(file_path)
            else:
                edit_message_text(chat_id, status_msg, "❌ آپلود فایل HTML با خطا مواجه شد.")
        else:
            edit_message_text(chat_id, status_msg, "❌ خطا در ذخیره صفحه وب")
    except Exception as e:
        logger.error(f"Webpage save error: {e}")
        try:
            edit_message_text(chat_id, status_msg, f"❌ خطا: {str(e)[:100]}")
        except:
            pass

def cleanup_files():
    for directory in [DOWNLOAD_DIR, TEMP_DIR]:
        for file in os.listdir(directory):
            file_path = os.path.join(directory, file)
            if os.path.isfile(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
    logger.info("Cleanup completed")

# ================= اجرای اصلی =================
def main():
    logger.info("🚀 راه‌اندازی ربات آپلودر...")
    init_db()
    try:
        requests.post(f"{BASE_URL}/setMyCommands", json={"commands": [{"command": "start", "description": "شروع ربات"}, {"command": "panel", "description": "پنل مدیریت"}]})
    except:
        pass
    logger.info(f"✅ ربات آماده! فقط کاربر {ALLOWED_USER_ID} مجاز است.")

    last_offset = 0
    while True:
        try:
            updates = get_updates(offset=last_offset + 1)
            if updates and updates.get("ok"):
                for update in updates.get("result", []):
                    last_offset = update["update_id"]
                    threading.Thread(target=handle_message, args=(update,), daemon=True).start()
            time.sleep(1)
        except KeyboardInterrupt:
            logger.info("👋 خاموش کردن ربات...")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
