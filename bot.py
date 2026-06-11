import os
import re
import time
import asyncio
import aiohttp
import logging
import tempfile
import functools
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
import subscriptions as subs
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# ─── INSTAGRAM COOKIES ────────────────────────────────────
INSTAGRAM_COOKIE_FILE = "/tmp/instagram_cookies.txt"

def _setup_instagram_cookies():
    cookies = os.environ.get("INSTAGRAM_COOKIES", "").strip()
    if not cookies:
        return None
    if "\n" not in cookies:
        cookies = re.sub(r' (#)', r'\n\1', cookies)
        cookies = re.sub(r' (\.instagram\.com\t)', r'\n\1', cookies)
    if not cookies.startswith("# Netscape"):
        cookies = "# Netscape HTTP Cookie File\n" + cookies
    with open(INSTAGRAM_COOKIE_FILE, "w") as f:
        f.write(cookies + "\n")
    logger.info(f"Instagram cookie file: {len([l for l in cookies.splitlines() if l.strip()])} lines written")
    return INSTAGRAM_COOKIE_FILE

_INSTAGRAM_COOKIE_PATH = _setup_instagram_cookies()

# ─── STATE ────────────────────────────────────────────────
WAITING_LINK = 1

# ─── INSTAGRAM RATE LIMIT ─────────────────────────────────
INSTA_COOLDOWN = 30  # seconds
_insta_last: dict[int, float] = {}  # user_id → last request timestamp

# ─── API CONFIG ───────────────────────────────────────────
SOCIAL_APIS = {
    "facebook":  "https://fb.watchdownload.com/api/download?url=",  # Working FB API
    "tiktok":    "https://tikwm.com/api/?url=",
    "spotify":   "https://spotifydl.the-zake.workers.dev/?url=",
    "snapchat":  "https://socialdownapi.anshapi.workers.dev/api/social-down?url=",
    "pinterest": "https://socialdownapi.anshapi.workers.dev/api/social-down?url=",
    "twitter":   "https://socialdownapi.anshapi.workers.dev/api/social-down?url=",
    "reddit":    "https://socialdownapi.anshapi.workers.dev/api/social-down?url=",
    "linkedin":  "https://socialdownapi.anshapi.workers.dev/api/social-down?url=",
    "terabox":   "https://terabox-player.netlify.app/api/download?url=",  # Backup TeraBox API
}

OTT_APIS = {
    "netflix":       "https://netflix.the-zake.workers.dev/?url=",
    "primevideo":    "https://primevideo.the-zake.workers.dev?url=",
    "zee5":          "https://zee5.the-zake.workers.dev?url=",
    "appletv":       "https://appletv.the-zake.workers.dev/url=",
    "airtelxstream": "https://airtelxstream.the-zake.workers.dev/?url=",
    "sunnxt":        "https://sunnxt.the-zake.workers.dev/?url=",
    "ahavideo":      "https://ahavideo.the-zake.workers.dev/?url=",
    "iqiyi":         "https://iqiyi.the-zake.workers.dev/?url=",
    "wetv":          "https://wetv.the-zake.workers.dev/?url=",
    "shemaroo":      "https://shemaroo.the-zake.workers.dev/?url=",
    "bookmyshow":    "https://bookmyshow.the-zake.workers.dev/?url=",
    "plextv":        "https://plextv.the-zake.workers.dev/?url=",
    "addatimes":     "https://addatimes.the-zake.workers.dev/?url=",
    "stage":         "https://stage.the-zake.workers.dev/?url=",
}

# ─── ANSH UNIVERSAL API ───────────────────────────────────
# Fallback for all platforms — supports 1000+ sites
ANSH_API_URL = "https://ansh-apis.is-dev.org/api/downloder?key=ansh&url="


async def _try_ansh_api(url: str) -> tuple[str | None, int, str]:
    """
    Call Ansh Universal Downloader API.
    Returns (download_url, size_bytes, quality_label) or (None, 0, '') on failure.
    """
    try:
        api_url = ANSH_API_URL + quote(url, safe="")
        async with aiohttp.ClientSession() as session:
            async with session.get(
                api_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Ansh API HTTP {resp.status} for {url[:60]}")
                    return None, 0, ""
                data = await resp.json(content_type=None)

        if not data or not isinstance(data, dict):
            return None, 0, ""

        # Try common response structures
        # Structure 1: {status: true/ok, url/link/download_url: ...}
        status = data.get("status") or data.get("success") or data.get("ok")
        if status is False or status == "error":
            logger.warning(f"Ansh API returned failure status for {url[:60]}")
            return None, 0, ""

        dl_url, size_bytes, quality_label = extract_best(data, prefer_audio=False)
        if dl_url:
            return dl_url, size_bytes, quality_label or "best"

        # Structure 2: direct url field at top level
        for key in ["url", "download_url", "downloadUrl", "link", "video_url", "videoUrl", "result"]:
            val = data.get(key)
            if val and isinstance(val, str) and val.startswith("http"):
                return val, 0, "best"

        # Structure 3: nested under data/result/response
        for wrapper in ["data", "result", "response", "video"]:
            inner = data.get(wrapper)
            if isinstance(inner, dict):
                dl_url, size_bytes, quality_label = extract_best(inner, prefer_audio=False)
                if dl_url:
                    return dl_url, size_bytes, quality_label or "best"
                for key in ["url", "download_url", "downloadUrl", "link"]:
                    val = inner.get(key)
                    if val and isinstance(val, str) and val.startswith("http"):
                        return val, 0, "best"

        logger.warning(f"Ansh API: no usable URL found in response keys={list(data.keys())}")
        return None, 0, ""

    except Exception as e:
        logger.warning(f"Ansh API exception: {str(e)[:150]}")
        return None, 0, ""


PLATFORM_EMOJIS = {
    "universal": "🌐",
    "tiktok": "🎵", "spotify": "🎧", "snapchat": "👻",
    "pinterest": "📌", "twitter": "🐦", "reddit": "🤖",
    "linkedin": "💼", "terabox": "📦",
    "netflix": "🔴", "primevideo": "🟡", "zee5": "🟣",
    "appletv": "⬛", "airtelxstream": "🔵", "sunnxt": "☀️",
    "ahavideo": "🎭", "iqiyi": "🟢", "wetv": "💚",
    "shemaroo": "🎪", "bookmyshow": "🎟️", "plextv": "🟠",
    "addatimes": "📺", "stage": "🎙️",
}

TG_MAX_SIZE = 50 * 1024 * 1024

PROGRESS_BARS = [
    ("⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜", "0%"),
    ("🟥⬜⬜⬜⬜⬜⬜⬜⬜⬜", "10%"),
    ("🟧🟧⬜⬜⬜⬜⬜⬜⬜⬜", "20%"),
    ("🟨🟨🟨⬜⬜⬜⬜⬜⬜⬜", "30%"),
    ("🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜", "40%"),
    ("🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜", "50%"),
    ("🟦🟦🟦🟦🟦🟦⬜⬜⬜⬜", "60%"),
    ("🟦🟦🟦🟦🟦🟦🟦⬜⬜⬜", "70%"),
    ("🟪🟪🟪🟪🟪🟪🟪🟪⬜⬜", "80%"),
    ("🟪🟪🟪🟪🟪🟪🟪🟪🟪⬜", "90%"),
    ("✅✅✅✅✅✅✅✅✅✅", "100%"),
]

# Social media domains — URLs from these are source links, not download links
_SOURCE_DOMAINS = re.compile(
    r"(tiktok\.com|instagram\.com|youtube\.com|youtu\.be|"
    r"facebook\.com|fb\.com|twitter\.com|x\.com|spotify\.com|"
    r"reddit\.com|linkedin\.com|pinterest\.com|snapchat\.com|"
    r"netflix\.com|primevideo\.com|zee5\.com|tv\.apple\.com)",
    re.IGNORECASE
)

# ─── HELPERS ──────────────────────────────────────────────
def detect_platform_from_url(url: str):
    patterns = {
        "youtube":    r"(youtube\.com|youtu\.be)",
        "instagram":  r"instagram\.com",
        "facebook":   r"(facebook\.com|fb\.com|fb\.watch)",
        "tiktok":     r"tiktok\.com",
        "spotify":    r"spotify\.com",
        "snapchat":   r"snapchat\.com",
        "pinterest":  r"(pinterest\.|pin\.it)",
        "twitter":    r"(twitter\.com|x\.com)",
        "reddit":     r"reddit\.com",
        "linkedin":   r"linkedin\.com",
        "terabox":    r"terabox\.com",
        "netflix":    r"netflix\.com",
        "primevideo": r"(primevideo\.com|amazon\.com/video)",
        "zee5":       r"zee5\.com",
        "appletv":    r"tv\.apple\.com",
        "airtelxstream": r"airtelxstream\.in",
        "sunnxt":     r"sunnxt\.com",
        "ahavideo":   r"aha\.video",
        "iqiyi":      r"iq\.com",
        "wetv":       r"wetv\.vip",
        "shemaroo":   r"shemarooent\.com",
        "bookmyshow": r"bookmyshow\.com",
        "plextv":     r"plex\.tv",
        "addatimes":  r"addatimes\.com",
        "stage":      r"stage\.in",
    }
    for platform, pattern in patterns.items():
        if re.search(pattern, url, re.IGNORECASE):
            return platform
    return None


def get_api_url(platform: str, user_url: str) -> str:
    base = SOCIAL_APIS.get(platform) or OTT_APIS.get(platform, "")
    return base + user_url


# Quality tier: higher = better. audio = -1
QUALITY_TIERS = {
    "audio": -1, "mp3": -1, "m4a": -1,
    "360": 1, "360p": 1, "low": 1, "watermark": 1,
    "480": 2, "480p": 2, "medium": 2, "sd": 2, "no_watermark": 2,
    "720": 3, "720p": 3, "hd": 3, "high": 3, "hq": 3, "hd_no_watermark": 3,
    "1080": 4, "1080p": 4, "fhd": 4, "fullhd": 4, "full_hd": 4,
    "2k": 5, "1440": 5, "4k": 6, "2160": 6,
}


def _quality_tier(qual: str, url: str) -> int:
    q = qual.lower().strip()
    if q in QUALITY_TIERS:
        return QUALITY_TIERS[q]
    for key, tier in QUALITY_TIERS.items():
        if key in q:
            return tier
    if "mp3" in url or "audio" in url:
        return -1
    return 2


def _is_download_url(url: str) -> bool:
    """Return True if the URL looks like an actual media download, not a source page."""
    # Allow known CDN/download domains
    if _SOURCE_DOMAINS.search(url):
        return False
    return True


def _collect_candidates(data: dict) -> list:
    """Recursively collect all {url, quality, size} from API response."""
    candidates = []

    def _extract_item(item: dict):
        url = (item.get("url") or item.get("link") or
               item.get("download_url") or item.get("downloadUrl") or
               item.get("src") or "")
        if not url or not url.startswith("http"):
            return
        # Skip source page URLs
        if not _is_download_url(url):
            return
        qual = str(item.get("quality") or item.get("resolution") or
                   item.get("format_id") or item.get("label") or
                   item.get("extension") or "")
        size = (item.get("size") or item.get("filesize") or
                item.get("contentLength") or item.get("data_size") or 0)
        if isinstance(size, str):
            try:
                size = int(size)
            except Exception:
                size = 0
        candidates.append({"url": url, "size": size, "quality": qual})

    def _walk(obj, depth=0):
        if depth > 6:
            return
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict) and ("url" in item or "link" in item or "src" in item):
                    _extract_item(item)
                else:
                    _walk(item, depth + 1)
        elif isinstance(obj, dict):
            if ("url" in obj or "link" in obj) and ("quality" in obj or "extension" in obj):
                _extract_item(obj)
            for v in obj.values():
                if isinstance(v, (list, dict)):
                    _walk(v, depth + 1)

    _walk(data)

    # Fallback: grab any download-looking top-level URL
    if not candidates:
        for key in ["download_url", "downloadUrl", "videoUrl", "video_url", "url"]:
            u = data.get(key)
            if not u and isinstance(data.get("data"), dict):
                u = data["data"].get(key)
            if u and isinstance(u, str) and u.startswith("http") and _is_download_url(u):
                candidates.append({"url": u, "size": 0, "quality": "best"})
                break

    return candidates


def extract_best(data: dict, prefer_audio: bool = False):
    """Return (url, size_bytes, quality_label) for the best available media."""
    candidates = _collect_candidates(data)
    if not candidates:
        return None, 0, None

    for c in candidates:
        c["tier"] = _quality_tier(c["quality"], c["url"])

    audio_cands = [c for c in candidates if c["tier"] == -1]
    video_cands = [c for c in candidates if c["tier"] != -1]

    if prefer_audio:
        pool = audio_cands if audio_cands else candidates
    else:
        pool = video_cands if video_cands else candidates

    if not pool:
        pool = candidates

    # Pick highest tier; break ties by largest size
    pool.sort(key=lambda c: (c["tier"], c["size"]), reverse=True)
    best = pool[0]
    return best["url"], best["size"], (best["quality"] or "best")


# ─── YOUTUBE via yt-dlp ───────────────────────────────────
_ytdlp_executor = ThreadPoolExecutor(max_workers=2)


def _ytdlp_get_info(url: str) -> dict:
    """Fetch YouTube stream URL using yt-dlp with multiple fallback strategies."""
    import yt_dlp

    # Strategy 1: Android client (bypasses most bot detection)
    ydl_opts_android = {
        "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["android"],
            }
        },
        "http_headers": {
            "User-Agent": "com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip",
        },
    }

    # Strategy 2: Web client fallback
    ydl_opts_web = {
        "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    for attempt, opts in enumerate([ydl_opts_android, ydl_opts_web], start=1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    continue

                # Prefer a direct URL (not a manifest)
                dl_url = info.get("url", "")
                if not dl_url and info.get("requested_formats"):
                    # Muxed format — use the video part URL (best we can do without ffmpeg)
                    dl_url = info["requested_formats"][0].get("url", "")

                if not dl_url:
                    continue

                resolution = info.get("format_note") or info.get("resolution") or f"{info.get('height', '')}p" or "best"
                filesize = info.get("filesize") or info.get("filesize_approx") or 0
                title = info.get("title", "")
                logger.info(f"YouTube OK via yt-dlp (attempt {attempt}), quality={resolution}, title={title[:40]}")
                return {
                    "url": dl_url, "ext": "mp4",
                    "quality_label": resolution,
                    "filesize": filesize, "title": title, "error": None
                }
        except Exception as e:
            logger.warning(f"YouTube yt-dlp attempt {attempt} failed: {str(e)[:150]}")
            continue

    return {"url": "", "ext": "mp4", "quality_label": "best",
            "filesize": 0, "title": "", "error": "All YouTube strategies failed. Try again later."}


def _ytdlp_get_info_generic(url: str, platform: str = "") -> dict:
    """Generic yt-dlp fallback for any platform (Facebook, etc)."""
    import yt_dlp
    ydl_opts = {
        "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return {"url": "", "quality_label": "best", "filesize": 0, "error": "No info returned"}
            dl_url = info.get("url", "")
            if not dl_url and info.get("requested_formats"):
                dl_url = info["requested_formats"][0].get("url", "")
            if not dl_url:
                return {"url": "", "quality_label": "best", "filesize": 0, "error": "No URL in info"}
            resolution = info.get("format_note") or f"{info.get('height', '')}p" or "best"
            filesize = info.get("filesize") or info.get("filesize_approx") or 0
            return {"url": dl_url, "quality_label": resolution, "filesize": filesize, "error": None}
    except Exception as e:
        return {"url": "", "quality_label": "best", "filesize": 0, "error": str(e)}


# ─── PROGRESS ─────────────────────────────────────────────
async def update_progress(msg, platform, frame_idx, status_text):
    bar, pct = PROGRESS_BARS[frame_idx]
    emoji = PLATFORM_EMOJIS.get(platform, "🎬")
    try:
        await msg.edit_text(
            f"⚙️ *Downloading...*\n\n"
            f"{emoji} *{platform.upper()}*\n\n"
            f"`{bar}` *{pct}*\n\n"
            f"_{status_text}_",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        pass


# ─── KEYBOARDS ────────────────────────────────────────────
def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 YouTube", callback_data="platform_youtube"),
            InlineKeyboardButton("📸 Instagram", callback_data="platform_instagram"),
        ],
        [
            InlineKeyboardButton("👥 Facebook", callback_data="platform_facebook"),
            InlineKeyboardButton("🎵 TikTok", callback_data="platform_tiktok"),
        ],
        [
            InlineKeyboardButton("🎧 Spotify", callback_data="platform_spotify"),
            InlineKeyboardButton("👻 Snapchat", callback_data="platform_snapchat"),
        ],
        [
            InlineKeyboardButton("📌 Pinterest", callback_data="platform_pinterest"),
            InlineKeyboardButton("🐦 Twitter/X", callback_data="platform_twitter"),
        ],
        [
            InlineKeyboardButton("🤖 Reddit", callback_data="platform_reddit"),
            InlineKeyboardButton("💼 LinkedIn", callback_data="platform_linkedin"),
        ],
        [
            InlineKeyboardButton("📦 TeraBox", callback_data="platform_terabox"),
        ],
        [
            InlineKeyboardButton("🌐 Universal Downloader", callback_data="platform_universal"),
        ],
        [InlineKeyboardButton("━━━━━ 🎬 OTT Platforms ━━━━━", callback_data="noop")],
        [
            InlineKeyboardButton("🔴 Netflix", callback_data="platform_netflix"),
            InlineKeyboardButton("🟡 Prime Video", callback_data="platform_primevideo"),
        ],
        [
            InlineKeyboardButton("🟣 Zee5", callback_data="platform_zee5"),
            InlineKeyboardButton("⬛ Apple TV", callback_data="platform_appletv"),
        ],
        [
            InlineKeyboardButton("🔵 Airtel Xstream", callback_data="platform_airtelxstream"),
            InlineKeyboardButton("☀️ Sun NXT", callback_data="platform_sunnxt"),
        ],
        [
            InlineKeyboardButton("🎭 Aha Video", callback_data="platform_ahavideo"),
            InlineKeyboardButton("🟢 iQIYI", callback_data="platform_iqiyi"),
        ],
        [
            InlineKeyboardButton("💚 WeTV", callback_data="platform_wetv"),
            InlineKeyboardButton("🎪 Shemaroo", callback_data="platform_shemaroo"),
        ],
        [
            InlineKeyboardButton("🎟️ BookMyShow", callback_data="platform_bookmyshow"),
            InlineKeyboardButton("🟠 Plex TV", callback_data="platform_plextv"),
        ],
        [
            InlineKeyboardButton("📺 Adda Times", callback_data="platform_addatimes"),
            InlineKeyboardButton("🎙️ Stage", callback_data="platform_stage"),
        ],
    ])


def welcome_text():
    return (
        "╔══════════════════════════╗\n"
        "║   *Made by Aadarsh* 🦇   ║\n"
        "╚══════════════════════════╝\n\n"
        "🌐 *Universal Media Downloader Bot*\n\n"
        "Is bot se aap kisi bhi platform ka content "
        "directly *Telegram* mein download kar sakte ho ⚡\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📦 *Supported Platforms:*\n\n"
        "🎬 *YouTube* — Videos in best quality\n"
        "📸 *Instagram* — Reels & Posts\n"
        "🎵 *TikTok* — HD, No Watermark\n"
        "🎧 *Spotify* — Songs as MP3\n"
        "👥 *Facebook* — Videos & Reels\n"
        "🐦 *Twitter / X* — Videos & GIFs\n"
        "🤖 *Reddit* — Videos & Media\n"
        "📌 *Pinterest* — Videos & Images\n"
        "👻 *Snapchat* — Snaps & Stories\n"
        "💼 *LinkedIn* — Videos\n"
        "📦 *TeraBox* — Files\n"
        "🌐 *Universal* — 1000+ Sites\n\n"
        "🎬 *OTT Platforms:*\n"
        "🔴 Netflix  🟡 Prime Video  🟣 Zee5\n"
        "⬛ Apple TV  🔵 Airtel Xstream  ☀️ Sun NXT\n"
        "🎭 Aha  🟢 iQIYI  💚 WeTV  🎪 Shemaroo\n"
        "🎟️ BookMyShow  🟠 Plex TV  📺 Adda Times\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💎 *Buy this bot subscription from:*\n"
        "👉 [Aadarsh 🦇](https://t.me/aadi4uuu)\n"
        "📞 *Contact Admin:* @aadi4uuu\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👇 *Apna platform choose karo:*"
    )


def no_access_text():
    return (
        "╔══════════════════════════╗\n"
        "║   *Made by Aadarsh* 🦇   ║\n"
        "╚══════════════════════════╝\n\n"
        "🔒 *Access Required!*\n\n"
        "Is bot ko use karne ke liye *subscription* lena padega.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💎 *Buy Subscription From:*\n"
        "👉 [Aadarsh 🦇](https://t.me/aadi4uuu)\n\n"
        "📞 *Contact Admin:* @aadi4uuu\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


# ─── DOWNLOAD & SEND ──────────────────────────────────────
async def _download_and_send(
    update_or_query, progress_msg, platform: str,
    download_url: str, size_bytes: int,
    quality_label: str, chat_id: int, is_audio: bool = False
):
    suffix = ".mp3" if is_audio else ".mp4"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name

        downloaded = 0
        last_frame = 4
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.google.com/",
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(
                download_url, timeout=aiohttp.ClientTimeout(total=600)
            ) as resp:
                total = int(resp.headers.get("Content-Length", 0))

                if total and total > TG_MAX_SIZE:
                    await progress_msg.edit_text(
                        f"⚠️ *File bahut badi hai!*\n\n"
                        f"📦 Size: *{total/(1024*1024):.1f} MB* (Telegram limit: 50 MB)\n\n"
                        f"🔗 [Direct Download Link]({download_url})\n\n"
                        f"_Thanks to Aadarsh 🦇_",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return False

                if resp.status not in (200, 206):
                    await progress_msg.edit_text(
                        f"❌ *Download failed!*\n\nServer ne HTTP {resp.status} diya.\n\n_/start se dobara try karo_",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return False

                with open(tmp_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded / total
                            frame = min(int(pct * 6) + 4, 9)
                            if frame != last_frame:
                                last_frame = frame
                                dl_mb = downloaded / (1024 * 1024)
                                tot_mb = total / (1024 * 1024)
                                await update_progress(
                                    progress_msg, platform, frame,
                                    f"Downloading... {dl_mb:.1f}/{tot_mb:.1f} MB"
                                )

        await update_progress(progress_msg, platform, 10, "Telegram pe upload ho raha hai...")

        file_size_actual = os.path.getsize(tmp_path)
        if file_size_actual == 0:
            await progress_msg.edit_text(
                "❌ *Downloaded file khaali hai.*\n\n_/start se dobara try karo_",
                parse_mode=ParseMode.MARKDOWN
            )
            return False

        if file_size_actual > TG_MAX_SIZE:
            await progress_msg.edit_text(
                f"⚠️ *File Telegram limit se badi hai!*\n\n"
                f"📦 Size: *{file_size_actual/(1024*1024):.1f} MB*\n\n"
                f"🔗 [Direct Download Link]({download_url})\n\n"
                f"_Thanks to Aadarsh 🦇_",
                parse_mode=ParseMode.MARKDOWN
            )
            return False

        emoji = PLATFORM_EMOJIS.get(platform, "🎬")
        actual_size_str = f"{file_size_actual/(1024*1024):.1f} MB"
        caption = (
            f"{emoji} *{platform.upper()}* › *{quality_label}*\n"
            f"📦 Size: *{actual_size_str}*\n\n"
            f"_Thanks to Aadarsh 🦇_"
        )

        bot = (update_or_query.get_bot() if hasattr(update_or_query, "get_bot")
               else update_or_query.message.get_bot())

        with open(tmp_path, "rb") as f:
            if is_audio:
                await bot.send_audio(
                    chat_id=chat_id, audio=f,
                    caption=caption, parse_mode=ParseMode.MARKDOWN
                )
            else:
                await bot.send_video(
                    chat_id=chat_id, video=f,
                    caption=caption, parse_mode=ParseMode.MARKDOWN,
                    supports_streaming=True
                )

        await progress_msg.delete()
        return True

    except TelegramError as e:
        await progress_msg.edit_text(
            f"❌ *Telegram upload failed!*\n\n`{str(e)}`\n\n"
            f"🔗 [Direct Download Link]({download_url})\n\n"
            f"_Thanks to Aadarsh 🦇_",
            parse_mode=ParseMode.MARKDOWN
        )
        return False
    except Exception as e:
        logger.error(f"Download/send error: {e}")
        await progress_msg.edit_text(
            f"❌ *Error!*\n\n`{str(e)[:300]}`\n\n_/start se dobara try karo_",
            parse_mode=ParseMode.MARKDOWN
        )
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


# ─── HANDLERS ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user_id = update.effective_user.id

    if not subs.is_authorized(user_id):
        await update.message.reply_text(
            no_access_text(),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        return ConversationHandler.END

    await update.message.reply_text(
        welcome_text(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
        disable_web_page_preview=True
    )
    return WAITING_LINK


async def platform_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "noop":
        return WAITING_LINK

    if query.data == "back_menu":
        await query.edit_message_text(
            welcome_text(),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_keyboard()
        )
        return WAITING_LINK

    platform = query.data.replace("platform_", "")
    emoji = PLATFORM_EMOJIS.get(platform, "🎬")
    context.user_data["platform"] = platform

    if platform == "universal":
        await query.edit_message_text(
            "🌐 *Universal Downloader* selected!\n\n"
            "✅ 1000+ platforms support karta hai\n\n"
            "🔗 Ab apna link paste karo:",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.edit_message_text(
            f"{emoji} *{platform.upper()}* selected!\n\n"
            f"🔗 Ab apna link paste karo:",
            parse_mode=ParseMode.MARKDOWN
        )
    return WAITING_LINK


async def link_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Subscription check on every download attempt
    if not subs.is_authorized(user_id):
        await update.message.reply_text(
            no_access_text(),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        return ConversationHandler.END

    url = update.message.text.strip()

    if not url.startswith("http"):
        await update.message.reply_text(
            "❌ *Invalid link!*\nEk valid URL send karo.",
            parse_mode=ParseMode.MARKDOWN
        )
        return WAITING_LINK

    platform = context.user_data.get("platform") or detect_platform_from_url(url)
    if not platform:
        await update.message.reply_text(
            "❌ Platform detect nahi hua.\n/start karke platform pehle choose karo.",
            parse_mode=ParseMode.MARKDOWN
        )
        return WAITING_LINK

    context.user_data["platform"] = platform
    emoji = PLATFORM_EMOJIS.get(platform, "🎬")
    chat_id = update.message.chat_id

    # Send initial progress message
    progress_msg = await update.message.reply_text(
        f"⚙️ *Downloading...*\n\n"
        f"{emoji} *{platform.upper()}*\n\n"
        f"`{PROGRESS_BARS[0][0]}` *{PROGRESS_BARS[0][1]}*\n\n"
        f"_Link check ho rahi hai..._",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        # ── Instagram: tele-social API ────────────────────────────
        if platform == "instagram":
            user_id = update.message.from_user.id

            # Validate Instagram link
            insta_pattern = r"(https?://)?(www\.)?instagram\.com/(p|reel|tv)/[A-Za-z0-9\-_]+"
            if not re.search(insta_pattern, url):
                await progress_msg.edit_text(
                    "❌ *Invalid Instagram link!*\n\n"
                    "Sirf post, reel ya tv link bhejo.\n_/start se dobara try karo_",
                    parse_mode=ParseMode.MARKDOWN
                )
                context.user_data.clear()
                return WAITING_LINK

            # Rate limit check
            now = time.time()
            last = _insta_last.get(user_id, 0)
            if (now - last) < INSTA_COOLDOWN:
                left = int(INSTA_COOLDOWN - (now - last))
                await progress_msg.edit_text(
                    f"⏳ *Please wait {left}s* before downloading another Instagram video.",
                    parse_mode=ParseMode.MARKDOWN
                )
                context.user_data.clear()
                return WAITING_LINK

            _insta_last[user_id] = now

            await update_progress(progress_msg, platform, 2, "Instagram se download ho raha hai...")

            api_url = "https://tele-social.vercel.app/down?url=" + quote(url, safe="")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        api_url, timeout=aiohttp.ClientTimeout(total=25)
                    ) as resp:
                        data = await resp.json(content_type=None)
            except Exception as e:
                await progress_msg.edit_text(
                    f"❌ *Instagram API Error!*\n\n`{str(e)[:200]}`\n\n_/start se dobara try karo_",
                    parse_mode=ParseMode.MARKDOWN
                )
                context.user_data.clear()
                return ConversationHandler.END

            if not data or data.get("status") is not True:
                msg_text = data.get("Message", "Download failed") if data else "Empty response"
                await progress_msg.edit_text(
                    f"❌ *Instagram download failed!*\n\n`{msg_text}`\n\n"
                    "_Link public hai? Sirf public posts/reels kaam karte hain._\n"
                    "_/start se dobara try karo_",
                    parse_mode=ParseMode.MARKDOWN
                )
                context.user_data.clear()
                return ConversationHandler.END

            payload  = data.get("data", {})
            media    = payload.get("media", {})
            mtype    = payload.get("type", "")
            video_url = media.get("video")
            image_url = media.get("image")

            backup_keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔓 Backup 🔓", url="https://t.me/+CbzPEYwf22NiYmY1")
            ]])
            caption = "✅ *Downloaded successfully*\n\n_Thanks to Aadarsh 🦇_"

            await update_progress(progress_msg, platform, 8, "Telegram pe bhej raha hai...")

            try:
                if mtype == "video" and video_url:
                    await update.message.reply_video(
                        video=video_url,
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=backup_keyboard,
                        supports_streaming=True
                    )
                elif image_url:
                    await update.message.reply_photo(
                        photo=image_url,
                        caption=caption,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=backup_keyboard
                    )
                else:
                    raise Exception("No media URL in response")

                await progress_msg.delete()
            except Exception as e:
                await progress_msg.edit_text(
                    f"❌ *Upload failed!*\n\n`{str(e)[:200]}`\n\n_/start se dobara try karo_",
                    parse_mode=ParseMode.MARKDOWN
                )

            context.user_data.clear()
            return ConversationHandler.END

        # ── TikTok: tikwm.com dedicated handler ──────────────────
        if platform == "tiktok":
            await update_progress(progress_msg, platform, 2, "TikTok se info le raha hai...")
            tiktok_url = f"https://tikwm.com/api/?url={quote(url, safe='')}&hd=1"
            data = None
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        tiktok_url,
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=aiohttp.ClientTimeout(total=20)
                    ) as resp:
                        data = await resp.json(content_type=None)
            except Exception as e:
                await progress_msg.edit_text(
                    f"❌ *TikTok API Error!*\n\n`{str(e)[:200]}`\n\n_/start se dobara try karo_",
                    parse_mode=ParseMode.MARKDOWN
                )
                context.user_data.clear()
                return ConversationHandler.END

            # tikwm response: data.play (no watermark), data.wmplay (watermark)
            video_url = None
            if data and data.get("code") == 0:
                d = data.get("data", {})
                video_url = d.get("play") or d.get("wmplay") or d.get("hdplay")
                size_bytes = d.get("size", 0)

            if not video_url:
                err = data.get("msg", "No video found") if data else "Empty response"
                await progress_msg.edit_text(
                    f"❌ *TikTok download failed!*\n\n`{err}`\n\n_/start se dobara try karo_",
                    parse_mode=ParseMode.MARKDOWN
                )
                context.user_data.clear()
                return ConversationHandler.END

            await update_progress(progress_msg, platform, 4, "Download ho raha hai (HD)...")
            await _download_and_send(
                update, progress_msg, platform,
                video_url, size_bytes, "HD", chat_id, is_audio=False
            )
            context.user_data.clear()
            return ConversationHandler.END

        # ── YouTube: yt-dlp via Android VR bypass ─────────────────
        if platform == "youtube":
            await update_progress(progress_msg, platform, 2, "YouTube se info le raha hai...")
            loop = asyncio.get_event_loop()
            yt_info = await loop.run_in_executor(
                _ytdlp_executor,
                functools.partial(_ytdlp_get_info, url)
            )
            if yt_info["error"] or not yt_info["url"]:
                # Fallback: Ansh API for YouTube
                await update_progress(progress_msg, platform, 3, "Universal API se try kar raha hai...")
                ansh_url, ansh_size, ansh_quality = await _try_ansh_api(url)
                if ansh_url:
                    await update_progress(progress_msg, platform, 4, f"Download ho raha hai ({ansh_quality})...")
                    await _download_and_send(
                        update, progress_msg, platform,
                        ansh_url, ansh_size, ansh_quality, chat_id, is_audio=False
                    )
                    context.user_data.clear()
                    return ConversationHandler.END

                await progress_msg.edit_text(
                    f"❌ *YouTube download failed!*\n\n"
                    f"`{yt_info.get('error','Unknown error')[:300]}`\n\n"
                    f"_/start se dobara try karo_",
                    parse_mode=ParseMode.MARKDOWN
                )
                context.user_data.clear()
                return ConversationHandler.END

            ql = yt_info["quality_label"]
            await update_progress(progress_msg, platform, 4, f"Download ho raha hai ({ql})...")
            await _download_and_send(
                update, progress_msg, platform,
                yt_info["url"], yt_info["filesize"],
                ql, chat_id, is_audio=False
            )
            context.user_data.clear()
            return ConversationHandler.END

        # ── Facebook: dedicated handler ───────────────────────────
        if platform == "facebook":
            await update_progress(progress_msg, platform, 2, "Facebook se info le raha hai...")
            fb_apis = [
                f"https://fb.watchdownload.com/api/download?url={quote(url, safe='')}",
                f"https://facebook-reels-and-videos-downloader.p.rapidapi.com/app/main.php?url={quote(url, safe='')}",
                f"https://social-media-video-downloader.p.rapidapi.com/smvd/get/all?url={quote(url, safe='')}",
            ]
            data = None
            for fb_api in fb_apis:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            fb_api, timeout=aiohttp.ClientTimeout(total=20)
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json(content_type=None)
                                if data:
                                    break
                except Exception:
                    continue

            if not data:
                # Last resort: yt-dlp for Facebook
                await update_progress(progress_msg, platform, 3, "yt-dlp se try kar raha hai...")
                loop = asyncio.get_event_loop()
                yt_info = await loop.run_in_executor(
                    _ytdlp_executor,
                    functools.partial(_ytdlp_get_info_generic, url, "facebook")
                )
                if yt_info["error"] or not yt_info["url"]:
                    # Final fallback: Ansh API for Facebook
                    await update_progress(progress_msg, platform, 4, "Universal API se try kar raha hai...")
                    ansh_url, ansh_size, ansh_quality = await _try_ansh_api(url)
                    if ansh_url:
                        await _download_and_send(
                            update, progress_msg, platform,
                            ansh_url, ansh_size, ansh_quality, chat_id, is_audio=False
                        )
                        context.user_data.clear()
                        return ConversationHandler.END

                    await progress_msg.edit_text(
                        f"❌ *Facebook download failed!*\n\n"
                        f"`{yt_info.get('error', 'No video found')[:300]}`\n\n"
                        f"_Public video link use karo. /start se dobara try karo_",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    context.user_data.clear()
                    return ConversationHandler.END
                await update_progress(progress_msg, platform, 4, f"Download ho raha hai...")
                await _download_and_send(
                    update, progress_msg, platform,
                    yt_info["url"], yt_info["filesize"],
                    yt_info["quality_label"], chat_id, is_audio=False
                )
                context.user_data.clear()
                return ConversationHandler.END

            download_url, size_bytes, quality_label = extract_best(data, prefer_audio=False)
            if not download_url:
                await progress_msg.edit_text(
                    "❌ *Facebook download link nahi mila.*\n\n"
                    "_Public video hai? /start se dobara try karo._",
                    parse_mode=ParseMode.MARKDOWN
                )
                context.user_data.clear()
                return ConversationHandler.END
            await update_progress(progress_msg, platform, 4, f"Download ho raha hai ({quality_label})...")
            await _download_and_send(
                update, progress_msg, platform,
                download_url, size_bytes, quality_label, chat_id, is_audio=False
            )
            context.user_data.clear()
            return ConversationHandler.END

        # ── Universal Downloader: Ansh API directly ───────────────
        if platform == "universal":
            await update_progress(progress_msg, platform, 2, "Universal API se info le raha hai...")
            download_url, size_bytes, quality_label = await _try_ansh_api(url)

            if not download_url:
                await progress_msg.edit_text(
                    "❌ *Universal Downloader failed!*\n\n"
                    "_Is link ka media nahi mila._\n"
                    "_Link public hai? /start se dobara try karo._",
                    parse_mode=ParseMode.MARKDOWN
                )
                context.user_data.clear()
                return ConversationHandler.END

            await update_progress(progress_msg, platform, 4, f"Download ho raha hai ({quality_label})...")
            await _download_and_send(
                update, progress_msg, platform,
                download_url, size_bytes, quality_label, chat_id, is_audio=False
            )
            context.user_data.clear()
            return ConversationHandler.END

        # ── TeraBox: dedicated handler ────────────────────────────
        if platform == "terabox":
            await update_progress(progress_msg, platform, 2, "TeraBox se info le raha hai...")

            download_url = None
            size_bytes = 0

            # API 1: terabox.udayscriptsx (most reliable)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"https://terabox.udayscriptsx.workers.dev/?url={quote(url, safe='')}",
                        timeout=aiohttp.ClientTimeout(total=20)
                    ) as resp:
                        if resp.status == 200:
                            d = await resp.json(content_type=None)
                            if isinstance(d, dict):
                                download_url = (d.get("downloadLink") or d.get("dlink") or
                                                d.get("download_link") or d.get("url") or
                                                (d.get("data") or {}).get("downloadLink") or
                                                (d.get("data") or {}).get("dlink"))
            except Exception:
                pass

            # API 2: teraboxapp.xyz
            if not download_url:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"https://teraboxapp.xyz/api?url={quote(url, safe='')}",
                            headers={"User-Agent": "Mozilla/5.0"},
                            timeout=aiohttp.ClientTimeout(total=20)
                        ) as resp:
                            if resp.status == 200:
                                d = await resp.json(content_type=None)
                                if isinstance(d, dict):
                                    download_url = (d.get("downloadLink") or d.get("dlink") or
                                                    d.get("download_link") or d.get("url") or
                                                    (d.get("data") or {}).get("downloadLink") or
                                                    (d.get("data") or {}).get("dlink"))
                                    size_bytes = d.get("size", 0) or (d.get("data") or {}).get("size", 0)
                except Exception:
                    pass

            # API 3: terabox-dl via alternative endpoint
            if not download_url:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            "https://terabox.hnn.workers.dev/api",
                            json={"url": url},
                            headers={"Content-Type": "application/json"},
                            timeout=aiohttp.ClientTimeout(total=20)
                        ) as resp:
                            if resp.status == 200:
                                d = await resp.json(content_type=None)
                                if isinstance(d, dict):
                                    download_url = (d.get("downloadLink") or d.get("dlink") or
                                                    d.get("url") or d.get("download_url"))
                except Exception:
                    pass

            if not download_url:
                await progress_msg.edit_text(
                    "❌ *TeraBox download failed!*\n\n"
                    "_Sabhi APIs fail ho gaye. Link public shared hai? TeraBox app se share karo._\n"
                    "_/start se dobara try karo_",
                    parse_mode=ParseMode.MARKDOWN
                )
                context.user_data.clear()
                return ConversationHandler.END

            await update_progress(progress_msg, platform, 4, "Download ho raha hai...")
            await _download_and_send(
                update, progress_msg, platform,
                download_url, size_bytes, "best", chat_id, is_audio=False
            )
            context.user_data.clear()
            return ConversationHandler.END


        # Spotify — always audio
        is_audio = (platform == "spotify")

        download_url = None
        size_bytes = 0
        quality_label = "best"

        # ── Step 1: Try existing platform-specific API (if configured) ──
        api_url = get_api_url(platform, url)
        if api_url.startswith("http"):
            await update_progress(progress_msg, platform, 2, "API se media info le raha hai...")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        api_url, timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        data = await resp.json(content_type=None)

                if data and not (isinstance(data, dict) and data.get("success") is False):
                    download_url, size_bytes, quality_label = extract_best(
                        data, prefer_audio=is_audio
                    )
                    if download_url:
                        logger.info(f"Platform API success for {platform}: {download_url[:60]}")
            except Exception as e:
                logger.warning(f"Platform API failed for {platform}: {str(e)[:100]}")

        # ── Step 2: Ansh Universal API fallback ──────────────────────────
        if not download_url:
            await update_progress(progress_msg, platform, 3, "Universal API se try kar raha hai...")
            logger.info(f"Trying Ansh API for platform={platform}, url={url[:60]}")
            download_url, size_bytes, quality_label = await _try_ansh_api(url)
            if download_url:
                logger.info(f"Ansh API success for {platform}: {download_url[:60]}")

        # ── No URL found from any source ─────────────────────────────────
        if not download_url:
            await progress_msg.edit_text(
                f"❌ *Download link nahi mila.*\n\n"
                f"_Sabhi APIs ne koi media nahi diya._\n"
                f"_Link public hai? /start se dobara try karo._",
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data.clear()
            return ConversationHandler.END

        await update_progress(progress_msg, platform, 4, f"Download ho raha hai ({quality_label})...")
        await _download_and_send(
            update, progress_msg, platform,
            download_url, size_bytes,
            quality_label, chat_id, is_audio=is_audio
        )

    except Exception as e:
        logger.error(f"link_received unhandled error: {e}")
        try:
            await progress_msg.edit_text(
                f"❌ *Unexpected error!*\n\n`{str(e)[:300]}`\n\n_/start se dobara try karo_",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ *Cancelled!*\n\n/start se dobara shuru karo.",
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END


# ─── MAIN ─────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable nahi mila!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_LINK: [
                CallbackQueryHandler(platform_selected, pattern="^(platform_|back_menu|noop)"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, link_received),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)

    logger.info("Bot chal raha hai... 🦇")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
