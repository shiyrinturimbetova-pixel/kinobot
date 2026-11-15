"""Ujas Kinolar boti (Telegram kino bot). Render + MongoDB + UptimeRobot uchun."""
import asyncio
import html
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from pymongo import MongoClient, ReturnDocument
from telegram import (
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Markup,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kinobot")

# ======================= SOZLAMALAR (Render Environment) =======================
TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
OWNER_ID = int(os.environ["ADMIN_ID"])  # asosiy admin
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "0"))  # kinolar saqlanadigan yopiq baza kanal
PORT = int(os.environ.get("PORT", "10000"))

UZ = timezone(timedelta(hours=5))  # O'zbekiston vaqti
PANEL_BTN = "🗄 Boshqaruv"
HTML = ParseMode.HTML
NOPREV = LinkPreviewOptions(is_disabled=True)

# ============================== BAZA ==============================
_db = MongoClient(MONGO_URI, tz_aware=True)["kinobot"]
users = _db["users"]
movies = _db["movies"]
counters = _db["counters"]
admins = _db["admins"]
channels = _db["channels"]
downloads = _db["downloads"]
cfg = _db["settings"]

S = cfg.find_one({"_id": "main"}) or {"_id": "main"}
S.setdefault("protect_regular", True)
S.setdefault("protect_premium", True)
S.setdefault("texts", {})
S.setdefault("start", {})
S.setdefault("user_menu", [])

ADMIN_IDS = {OWNER_ID} | {a["_id"] for a in admins.find()}

# Dizayn bo'limida o'zgartiriladigan matnlar: kalit -> (nomi, standart, html_mi, turi)
ITEMS = {
    "not_found": ("Hech narsa topilmadi xabari", "Hech narsa topilmadi 😕", True, "t"),
    "code_not_found": ("Kod topilmadi xabari", "Bu kod bo'yicha kino topilmadi 😕", True, "t"),
    "found_list": ("Qidiruv natijasi sarlavhasi", "Topilganlar:", True, "t"),
    "banned_text": ("Bloklanganlarga xabar", "Siz botdan foydalanishdan bloklangansiz.", True, "t"),
    "caption_channel": ("Kino tagidagi kanal (@username)", "", False, "t"),
    "sub_text": (
        "Majburiy obuna xabari",
        "⚠️ Botdan to'liq foydalanish uchun quyidagi kanallarimizga obuna bo'ling!",
        True,
        "s",
    ),
    "check": ("Tekshirish tugmasi", "✅ Tekshirish", False, "b"),
    "watch": ("Tomosha qilish tugmasi", "✨Tomosha Qilish✨", False, "b"),
}
DEFAULT_START = "Salom! Kino nomini yoki kodini yozing, men topib beraman."


def save_settings():
    cfg.replace_one({"_id": "main"}, S, upsert=True)


def T(key: str) -> str:
    return S["texts"].get(key) or ITEMS[key][1]


# ============================== YORDAMCHILAR ==============================
def now():
    return datetime.now(timezone.utc)


def uz_midnight():
    n = datetime.now(UZ)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def fmt_dt(dt):
    return dt.astimezone(UZ).strftime("%d.%m.%Y %H:%M") if dt else "—"


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def next_code() -> int:
    doc = counters.find_one_and_update(
        {"_id": "movies"}, {"$inc": {"n": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return doc["n"]


def touch_user(update: Update):
    u = update.effective_user
    if not u or u.is_bot:
        return None
    return users.find_one_and_update(
        {"_id": u.id},
        {
            "$set": {
                "first_name": u.first_name or "",
                "username": u.username or "",
                "is_premium": bool(u.is_premium),
                "last_active": now(),
                "left": False,
            },
            "$setOnInsert": {"joined": now(), "banned": False},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


def user_keyboard(uid: int):
    labels = [b["label"] for b in S["user_menu"]]
    rows = [labels[i : i + 2] for i in range(0, len(labels), 2)]
    if is_admin(uid):
        rows.append([PANEL_BTN])
    if not rows:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def show(update: Update, text: str, kb=None):
    """Tugmadan kelgan bo'lsa xabarni tahrirlaydi, aks holda yangi xabar yuboradi."""
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=HTML, link_preview_options=NOPREV)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            try:
                await q.message.delete()
            except TelegramError:
                pass
            await q.message.chat.send_message(text, reply_markup=kb, parse_mode=HTML, link_preview_options=NOPREV)
    else:
        await update.effective_chat.send_message(text, reply_markup=kb, parse_mode=HTML, link_preview_options=NOPREV)


async def safe_answer(q, text=None, alert=False):
    try:
        await q.answer(text, show_alert=alert)
    except TelegramError:
        pass


def paginate_nav(prefix: str, page: int, pages: int):
    row = []
    if page > 0:
        row.append(Btn("◀️", callback_data=f"{prefix}:{page - 1}"))
    row.append(Btn(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        row.append(Btn("▶️", callback_data=f"{prefix}:{page + 1}"))
    return row


def pages_of(total: int, per: int) -> int:
    return max(1, -(-total // per))


# ============================== VEB-SERVER (UptimeRobot) ==============================
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_web():
    HTTPServer(("0.0.0.0", PORT), Ping).serve_forever()


# ============================== MAJBURIY OBUNA ==============================
async def not_subscribed(bot, uid: int):
    missing = []
    for ch in channels.find():
        try:
            m = await bot.get_chat_member(ch["_id"], uid)
            if m.status in ("left", "kicked") or (m.status == "restricted" and not m.is_member):
                missing.append(ch)
        except TelegramError as e:
            log.warning("Obunani tekshirib bo'lmadi (%s): %s", ch["_id"], e)
    return missing


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE, code: int = 0) -> bool:
    """Obuna bo'lmagan bo'lsa to'sadi. Adminlar tekshirilmaydi."""
    uid = update.effective_user.id
    if is_admin(uid):
        return True
    missing = await not_subscribed(ctx.bot, uid)
    if not missing:
        return True
    rows = [[Btn(c["title"], url=c["link"])] for c in missing]
    rows.append([Btn(T("check"), callback_data=f"chk:{code}")])
    await ctx.bot.send_message(
        update.effective_chat.id, T("sub_text"), parse_mode=HTML, reply_markup=Markup(rows)
    )
    return False


# ============================== KINO YUBORISH ==============================
async def deliver(bot, chat_id: int, uid: int, code: int, premium: bool):
    m = movies.find_one({"code": code})
    if not m or not CHANNEL_ID:
        await bot.send_message(chat_id, T("code_not_found"), parse_mode=HTML)
        return
    protect = S["protect_premium"] if premium else S["protect_regular"]
    caption = m["title"]
    if T("caption_channel"):
        caption += f"\nKanal: {T('caption_channel')}"
    try:
        await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=CHANNEL_ID,
            message_id=m["message_id"],
            caption=caption,
            protect_content=protect,
        )
        downloads.insert_one({"user": uid, "code": code, "at": now()})
    except TelegramError as e:
        log.error("Kino yuborilmadi (%s): %s", code, e)
        await bot.send_message(chat_id, "Kinoni yuborib bo'lmadi, keyinroq urinib ko'ring.")


async def send_start(bot, chat_id: int, uid: int):
    st = S["start"]
    kbd = user_keyboard(uid)
    text = st.get("html") or esc(DEFAULT_START)
    kind = st.get("type", "text")
    if kind == "photo" and st.get("file_id"):
        await bot.send_photo(chat_id, st["file_id"], caption=text, parse_mode=HTML, reply_markup=kbd)
    elif kind == "video" and st.get("file_id"):
        await bot.send_video(chat_id, st["file_id"], caption=text, parse_mode=HTML, reply_markup=kbd)
    else:
        await bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kbd)


async def user_text(update: Update, ctx, text: str, doc):
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    if text.isdigit():
        await deliver(ctx.bot, chat_id, uid, int(text), bool(doc.get("is_premium")))
        return
    q = text.casefold()
    found = [m for m in movies.find().sort("code", 1) if q in m["title"].casefold()][:30]
    if not found:
        await update.message.reply_text(T("not_found"), parse_mode=HTML)
        return
    rows = [[Btn(m["title"][:60], callback_data=f"m:{m['code']}")] for m in found]
    await update.message.reply_text(T("found_list"), parse_mode=HTML, reply_markup=Markup(rows))


# ============================== ASOSIY HANDLERLAR ==============================
async def on_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = touch_user(update)
    uid = update.effective_user.id
    ctx.user_data.clear()
    if doc.get("banned") and not is_admin(uid):
        await update.message.reply_text(T("banned_text"), parse_mode=HTML)
        return
    code = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 0
    if not await gate(update, ctx, code):
        return
    if code:
        await deliver(ctx.bot, update.effective_chat.id, uid, code, bool(doc.get("is_premium")))
    else:
        await send_start(ctx.bot, update.effective_chat.id, uid)


async def on_admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    touch_user(update)
    if is_admin(update.effective_user.id):
        ctx.user_data.clear()
        await v_panel(update)


async def on_private(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    if not m:
        return
    uid = update.effective_user.id
    doc = touch_user(update)
    if is_admin(uid):
        st = ctx.user_data.get("st")
        if st and st.get("k") in STATES:
            await STATES[st["k"]](update, ctx, st)
            return
        if m.text == PANEL_BTN:
            await v_panel(update)
            return
    elif doc.get("banned"):
        await m.reply_text(T("banned_text"), parse_mode=HTML)
        return
    if not m.text:
        return
    for b in S["user_menu"]:
        if b["label"] == m.text:
            await m.reply_text(b["text"], parse_mode=HTML)
            return
    if not await gate(update, ctx):
        return
    await user_text(update, ctx, m.text.strip(), doc)


async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Baza kanalga video tashlansa: caption = kino nomi. Bot kod beradi."""
    post = update.channel_post
    chat_id = post.chat.id
    if CHANNEL_ID == 0:
        await ctx.bot.send_message(chat_id, f"Kanal ID: {chat_id}\nUni Render'da CHANNEL_ID ga yozing.")
        return
    if chat_id != CHANNEL_ID or not (post.video or post.document):
        return
    title = (post.caption or "").strip()
    if not title:
        await ctx.bot.send_message(chat_id, "⚠️ Videoga kino nomini caption qilib yozing.")
        return
    code = next_code()
    movies.insert_one({"code": code, "title": title, "message_id": post.message_id, "created": now()})
    link = f"https://t.me/{ctx.bot.username}?start={code}"
    await ctx.bot.send_message(
        chat_id,
        f"✅ Saqlandi: {title}\nKod: {code}\nHavola: {link}",
        reply_to_message_id=post.message_id,
    )


async def on_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchi botni bloklasa yoki blokdan chiqarsa."""
    cm = update.my_chat_member
    if cm and cm.chat.type == "private":
        users.update_one({"_id": cm.from_user.id}, {"$set": {"left": cm.new_chat_member.status == "kicked"}})


# ============================== KO'RINISHLAR (VIEWS) ==============================
async def v_panel(update: Update):
    await show(
        update,
        "<b>Admin paneliga xush kelibsiz!</b>\n\nBo'limni tanlang 👇",
        Markup(
            [
                [Btn("🎬 Kinolar", callback_data="mv:home"), Btn("📊 Statistika", callback_data="st:show")],
                [Btn("📢 Kanallar", callback_data="ch:home"), Btn("📨 Xabar yuborish", callback_data="bc:home")],
                [Btn("👥 Foydalanuvchilar", callback_data="us:home"), Btn("🎨 Dizayn", callback_data="ds:home")],
                [Btn("⚙️ Sozlamalar", callback_data="se:home")],
                [Btn("◀️ Orqaga", callback_data="a:close")],
            ]
        ),
    )


# ---------- Kinolar ----------
async def v_movies(update):
    n = movies.count_documents({})
    await show(
        update,
        f"🎬 <b>Kinolar bo'limi</b>\n\nBazada: {n} ta kino\n\n"
        "<i>Yangi kino qo'shish uchun videoni baza kanalga caption (nomi) bilan tashlang.</i>",
        Markup(
            [
                [Btn("📋 Ro'yxat", callback_data="mv:l:0"), Btn("🔍 Qidirish", callback_data="mv:s")],
                [Btn("◀️ Orqaga", callback_data="a:home")],
            ]
        ),
    )


async def v_movie_list(update, page: int):
    per = 10
    total = movies.count_documents({})
    pages = pages_of(total, per)
    page = max(0, min(page, pages - 1))
    docs = list(movies.find().sort("code", -1).skip(page * per).limit(per))
    rows = [[Btn(f"{m['code']}. {m['title']}"[:60], callback_data=f"mv:c:{m['code']}")] for m in docs]
    rows.append(paginate_nav("mv:l", page, pages))
    rows.append([Btn("◀️ Orqaga", callback_data="mv:home")])
    await show(update, f"📋 <b>Kinolar ro'yxati</b>\nJami: {total} ta", Markup(rows))


async def v_movie_card(update, code: int):
    m = movies.find_one({"code": code})
    if not m:
        await show(update, "Kino topilmadi.", Markup([[Btn("◀️ Orqaga", callback_data="mv:l:0")]]))
        return
    dl = downloads.count_documents({"code": code})
    await show(
        update,
        f"🎬 <b>{esc(m['title'])}</b>\n\nKod: <code>{code}</code>\nQo'shilgan: {fmt_dt(m.get('created'))}\n"
        f"Yuklanishlar: {dl} ta",
        Markup(
            [
                [Btn("✏️ Nomini o'zgartirish", callback_data=f"mv:r:{code}")],
                [Btn("🗑 O'chirish", callback_data=f"mv:d:{code}")],
                [Btn("◀️ Orqaga", callback_data="mv:l:0")],
            ]
        ),
    )


async def cb_mv(update, ctx, p):
    a = p[1]
    if a == "home":
        await v_movies(update)
    elif a == "l":
        await v_movie_list(update, int(p[2]))
    elif a == "c":
        await v_movie_card(update, int(p[2]))
    elif a == "s":
        ctx.user_data["st"] = {"k": "mv_search"}
        await show(update, "🔍 Kino kodini yoki nomini yozing:", Markup([[Btn("◀️ Orqaga", callback_data="mv:home")]]))
    elif a == "r":
        ctx.user_data["st"] = {"k": "mv_rename", "code": int(p[2])}
        await show(update, "✏️ Yangi nomni yozing:", Markup([[Btn("◀️ Orqaga", callback_data=f"mv:c:{p[2]}")]]))
    elif a == "d":
        await show(
            update,
            "Rostdan ham o'chirasizmi?\n\n<i>Kino botdan o'chadi, baza kanaldagi video esa qoladi.</i>",
            Markup(
                [
                    [Btn("✅ Ha, o'chirish", callback_data=f"mv:dy:{p[2]}")],
                    [Btn("◀️ Yo'q", callback_data=f"mv:c:{p[2]}")],
                ]
            ),
        )
    elif a == "dy":
        movies.delete_one({"code": int(p[2])})
        await v_movie_list(update, 0)


STATES = {}


def state(name):
    def deco(fn):
        STATES[name] = fn
        return fn

    return deco


@state("mv_search")
async def s_mv_search(update, ctx, st):
    t = (update.message.text or "").strip()
    if not t:
        return
    if t.isdigit():
        found = list(movies.find({"code": int(t)}))
    else:
        q = t.casefold()
        found = [m for m in movies.find().sort("code", 1) if q in m["title"].casefold()][:30]
    ctx.user_data.pop("st", None)
    if not found:
        await update.message.reply_text("Hech narsa topilmadi.")
        await v_movies(update)
        return
    rows = [[Btn(f"{m['code']}. {m['title']}"[:60], callback_data=f"mv:c:{m['code']}")] for m in found]
    rows.append([Btn("◀️ Orqaga", callback_data="mv:home")])
    await update.message.reply_text("Topilganlar:", reply_markup=Markup(rows))


@state("mv_rename")
async def s_mv_rename(update, ctx, st):
    t = (update.message.text or "").strip()
    if not t:
        return
    movies.update_one({"code": st["code"]}, {"$set": {"title": t}})
    ctx.user_data.pop("st", None)
    await update.message.reply_text("✅ Nomi o'zgartirildi.")
    await v_movie_card(update, st["code"])


# ---------- Statistika ----------
async def v_stats(update, ctx):
    n = now()
    today, d7, d30 = uz_midnight(), n - timedelta(days=7), n - timedelta(days=30)

    def cnt(col, field, since):
        return col.count_documents({field: {"$gte": since}})

    total = users.count_documents({})
    left = users.count_documents({"left": True})
    text = (
        "📊 <b>Bot statistikasi</b>\n"
        f"🗓 {datetime.now(UZ).strftime('%d.%m.%Y, %H:%M')}\n\n"
        "👥 <b>Foydalanuvchilar</b>\n"
        f"• Jami: {total} ta\n• Faol: {total - left} ta\n• Botni bloklagan: {left} ta\n\n"
        "📈 <b>Yangi foydalanuvchilar</b>\n"
        f"• Bugun: +{cnt(users, 'joined', today)}\n• 7 kunda: +{cnt(users, 'joined', d7)}\n"
        f"• 30 kunda: +{cnt(users, 'joined', d30)}\n\n"
        "🔥 <b>Faollik (botdan foydalanganlar)</b>\n"
        f"• Bugun: {cnt(users, 'last_active', today)} ta\n• 7 kunda: {cnt(users, 'last_active', d7)} ta\n"
        f"• 30 kunda: {cnt(users, 'last_active', d30)} ta\n\n"
        "📥 <b>Kino yuklanishlari</b>\n"
        f"• Bugun: {cnt(downloads, 'at', today)} ta\n• 7 kunda: {cnt(downloads, 'at', d7)} ta\n"
        f"• 30 kunda: {cnt(downloads, 'at', d30)} ta\n\n"
        f"🎬 Bazadagi kinolar: {movies.count_documents({})} ta\n\n"
        f"🤖 @{ctx.bot.username}"
    )
    await show(
        update,
        text,
        Markup([[Btn("🔄 Yangilash", callback_data="st:show")], [Btn("◀️ Orqaga", callback_data="a:home")]]),
    )


# ---------- Kanallar (majburiy obuna) ----------
async def v_channels(update):
    n = channels.count_documents({})
    await show(
        update,
        f"🔐 <b>Majburiy obuna kanallar</b>\n\nJami: {n} ta",
        Markup(
            [
                [Btn("➕ Kanal qo'shish", callback_data="ch:add")],
                [Btn("📋 Ro'yxatni ko'rish", callback_data="ch:list")],
                [Btn("🗑 Kanalni o'chirish", callback_data="ch:del")],
                [Btn("◀️ Orqaga", callback_data="a:home")],
            ]
        ),
    )


async def resolve_chat(update, ctx):
    """@username, ID yoki forward qilingan postdan kanalni aniqlaydi va botning adminligini tekshiradi."""
    m = update.message
    ref = None
    fo = m.forward_origin
    if fo is not None:
        c = getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)
        ref = c.id if c else None
    elif m.text:
        t = re.sub(r"^https?://t\.me/", "@", m.text.strip())
        if re.fullmatch(r"-?\d+", t):
            ref = int(t)
        elif re.fullmatch(r"@[A-Za-z0-9_]{4,}", t):
            ref = t
    if ref is None:
        await m.reply_text("❌ Aniqlay olmadim. @username yozing, ID yuboring yoki kanaldan post forward qiling.")
        return None
    try:
        chat = await ctx.bot.get_chat(ref)
        me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
    except TelegramError:
        await m.reply_text("❌ Topilmadi yoki bot u yerda yo'q. Botni avval admin qilib qo'shing.")
        return None
    if me.status not in ("administrator", "creator"):
        await m.reply_text("❌ Bot bu kanalda admin emas. Avval botni admin qiling.")
        return None
    return chat


async def cb_ch(update, ctx, p):
    a = p[1]
    if a == "home":
        await v_channels(update)
    elif a == "add":
        ctx.user_data["st"] = {"k": "ch_add"}
        await show(
            update,
            "➕ Kanalning <b>@username</b> ini yozing, ID sini yuboring yoki kanaldan biror postni forward qiling.\n\n"
            "<i>Bot kanalda admin bo'lishi shart.</i>",
            Markup([[Btn("◀️ Orqaga", callback_data="ch:home")]]),
        )
    elif a == "list":
        docs = list(channels.find())
        if not docs:
            text = "📋 <b>Ro'yxat bo'sh.</b>"
        else:
            lines = [
                f"{i}. {esc(c['title'])} — {('@' + c['username']) if c.get('username') else 'yopiq kanal'}"
                for i, c in enumerate(docs, 1)
            ]
            text = "📋 <b>Majburiy obuna kanallar</b>\n\n" + "\n".join(lines)
        await show(update, text, Markup([[Btn("◀️ Orqaga", callback_data="ch:home")]]))
    elif a == "del":
        docs = list(channels.find())
        if not docs:
            await show(update, "O'chirish uchun kanal yo'q.", Markup([[Btn("◀️ Orqaga", callback_data="ch:home")]]))
            return
        rows = [[Btn(f"🗑 {c['title']}"[:60], callback_data=f"ch:dc:{c['_id']}")] for c in docs]
        rows.append([Btn("◀️ Orqaga", callback_data="ch:home")])
        await show(update, "Qaysi kanalni o'chiramiz?", Markup(rows))
    elif a == "dc":
        c = channels.find_one({"_id": int(p[2])})
        title = esc(c["title"]) if c else "Kanal"
        await show(
            update,
            f"<b>{title}</b> ni ro'yxatdan o'chirasizmi?",
            Markup(
                [
                    [Btn("✅ Ha, o'chirish", callback_data=f"ch:dy:{p[2]}")],
                    [Btn("◀️ Yo'q", callback_data="ch:del")],
                ]
            ),
        )
    elif a == "dy":
        channels.delete_one({"_id": int(p[2])})
        await v_channels(update)


async def save_channel(update, ctx, chat, link):
    channels.replace_one(
        {"_id": chat.id},
        {"_id": chat.id, "title": chat.title or str(chat.id), "username": chat.username or "", "link": link},
        upsert=True,
    )
    ctx.user_data.pop("st", None)
    await update.message.reply_text(f"✅ Qo'shildi: {chat.title}")
    await v_channels(update)


@state("ch_add")
async def s_ch_add(update, ctx, st):
    chat = await resolve_chat(update, ctx)
    if not chat:
        return
    if channels.find_one({"_id": chat.id}):
        await update.message.reply_text("Bu kanal allaqachon qo'shilgan.")
        return
    link = f"https://t.me/{chat.username}" if chat.username else getattr(chat, "invite_link", None)
    if not link:
        try:
            link = await ctx.bot.export_chat_invite_link(chat.id)
        except TelegramError:
            link = None
    if not link:
        ctx.user_data["st"] = {"k": "ch_link", "id": chat.id, "title": chat.title or str(chat.id)}
        await update.message.reply_text("Bu yopiq kanal. Taklif havolasini yuboring (https://t.me/+...):")
        return
    await save_channel(update, ctx, chat, link)


@state("ch_link")
async def s_ch_link(update, ctx, st):
    t = (update.message.text or "").strip()
    if not t.startswith("https://t.me/"):
        await update.message.reply_text("❌ Havola https://t.me/ bilan boshlanishi kerak.")
        return
    channels.replace_one(
        {"_id": st["id"]},
        {"_id": st["id"], "title": st["title"], "username": "", "link": t},
        upsert=True,
    )
    ctx.user_data.pop("st", None)
    await update.message.reply_text(f"✅ Qo'shildi: {st['title']}")
    await v_channels(update)


# ---------- Xabar yuborish ----------
BC_INFO = (
    "📨 <b>Xabar yuborish</b>\n\nXabar foydalanuvchilarga qanday borsin?\n\n"
    "💬 <b>Oddiy</b> — xabar bot nomidan boradi. Kimdan kelgani ko'rinmaydi, xuddi bot o'zi yozgandek. "
    "Matn, rasm, video, fayl yuborish mumkin, ostiga tugma (havola) qo'shsa bo'ladi.\n\n"
    "📨 <b>Forward</b> — kanal posti asl manbasi bilan boradi. Ustida «Forwarded from Kanal nomi» yozuvi turadi, "
    "bosilsa kanal ochiladi. Kanalni reklama qilish uchun qulay. Tugma qo'shib bo'lmaydi."
)


async def v_bc_home(update):
    await show(
        update,
        BC_INFO,
        Markup(
            [
                [Btn("💬 Oddiy", callback_data="bc:n"), Btn("📨 Forward", callback_data="bc:f")],
                [Btn("◀️ Orqaga", callback_data="a:home")],
            ]
        ),
    )


def parse_buttons(text: str):
    rows = []
    for line in text.strip().splitlines():
        mt = re.match(r"^(.+?)\s*[-–—]\s*((?:https?://|t\.me/)\S+)$", line.strip())
        if not mt:
            return None
        url = mt.group(2)
        if url.startswith("t.me/"):
            url = "https://" + url
        rows.append((mt.group(1).strip(), url))
    return rows or None


def bc_markup(btns):
    return Markup([[Btn(t, url=u)] for t, u in btns]) if btns else None


async def bc_confirm(update, ctx):
    fl = ctx.user_data["fl"]
    n = users.count_documents({"left": {"$ne": True}, "banned": {"$ne": True}})
    chat = update.effective_chat
    if fl["mode"] == "n":
        await ctx.bot.copy_message(chat.id, fl["cid"], fl["mid"], reply_markup=bc_markup(fl.get("btns")))
    await chat.send_message(
        f"Yuqoridagi xabar <b>{n} ta</b> foydalanuvchiga yuboriladi. Tasdiqlaysizmi?",
        parse_mode=HTML,
        reply_markup=Markup(
            [
                [Btn("✅ Yuborish", callback_data="bc:go"), Btn("❌ Bekor qilish", callback_data="bc:cancel")],
                [Btn("◀️ Orqaga", callback_data="bc:n" if fl["mode"] == "n" else "bc:f")],
            ]
        ),
    )


async def run_broadcast(bot, admin_chat: int, fl: dict):
    ids = [u["_id"] for u in users.find({"left": {"$ne": True}, "banned": {"$ne": True}}, {"_id": 1})]
    markup = bc_markup(fl.get("btns"))
    ok = bad = 0
    for uid in ids:
        for _ in range(3):
            try:
                if fl["mode"] == "n":
                    await bot.copy_message(uid, fl["cid"], fl["mid"], reply_markup=markup)
                else:
                    await bot.forward_message(uid, fl["cid"], fl["mid"])
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Forbidden:
                users.update_one({"_id": uid}, {"$set": {"left": True}})
                bad += 1
                break
            except TelegramError:
                bad += 1
                break
        else:
            bad += 1
        await asyncio.sleep(0.05)
    await bot.send_message(
        admin_chat,
        f"✅ <b>Xabar yuborish yakunlandi</b>\n\nYuborildi: {ok} ta\nYuborilmadi: {bad} ta\nJami: {len(ids)} ta",
        parse_mode=HTML,
    )


async def cb_bc(update, ctx, p):
    a = p[1]
    if a == "home":
        await v_bc_home(update)
    elif a == "n":
        ctx.user_data["fl"] = {"mode": "n"}
        ctx.user_data["st"] = {"k": "bc_msg"}
        await show(
            update,
            "💬 <b>Oddiy xabar</b>\n\nYuboriladigan xabarni yuboring (matn, rasm, video, fayl, audio, ovozli xabar, "
            "GIF, stiker). Qalin, kursiv va havola formatlari saqlanadi.",
            Markup([[Btn("◀️ Orqaga", callback_data="bc:home")]]),
        )
    elif a == "f":
        ctx.user_data["fl"] = {"mode": "f"}
        ctx.user_data["st"] = {"k": "bc_fwd"}
        await show(
            update,
            "📨 <b>Forward</b>\n\nKanaldan postni shu yerga forward qiling.",
            Markup([[Btn("◀️ Orqaga", callback_data="bc:home")]]),
        )
    elif a == "skip":
        fl = ctx.user_data.get("fl")
        if fl:
            fl["btns"] = []
            ctx.user_data.pop("st", None)
            await bc_confirm(update, ctx)
    elif a == "go":
        fl = ctx.user_data.pop("fl", None)
        if not fl:
            return
        await show(update, "⏳ Yuborish boshlandi. Tugagach hisobot beraman.")
        ctx.application.create_task(run_broadcast(ctx.bot, update.effective_chat.id, fl))
    elif a == "cancel":
        ctx.user_data.pop("fl", None)
        await v_bc_home(update)


@state("bc_msg")
async def s_bc_msg(update, ctx, st):
    m = update.message
    ctx.user_data["fl"].update(mid=m.message_id, cid=m.chat_id)
    ctx.user_data["st"] = {"k": "bc_btn"}
    await m.reply_text(
        "Xabarning ostiga tugma qo'shasizmi?\n\nHar bir qatorga bittadan yozing:\n"
        "<code>Tugma nomi - https://havola</code>",
        parse_mode=HTML,
        reply_markup=Markup(
            [
                [Btn("⏭ O'tkazib yuborish", callback_data="bc:skip")],
                [Btn("◀️ Orqaga", callback_data="bc:n")],
            ]
        ),
    )


@state("bc_btn")
async def s_bc_btn(update, ctx, st):
    btns = parse_buttons(update.message.text or "")
    if not btns:
        await update.message.reply_text("❌ Format noto'g'ri. Misol:\nKanalimiz - https://t.me/kanal")
        return
    ctx.user_data["fl"]["btns"] = btns
    ctx.user_data.pop("st", None)
    await bc_confirm(update, ctx)


@state("bc_fwd")
async def s_bc_fwd(update, ctx, st):
    m = update.message
    if m.forward_origin is None:
        await m.reply_text("❌ Bu forward qilingan post emas. Kanaldan postni forward qiling.")
        return
    ctx.user_data["fl"].update(mid=m.message_id, cid=m.chat_id)
    ctx.user_data.pop("st", None)
    await bc_confirm(update, ctx)


# ---------- Foydalanuvchilar ----------
async def v_users(update):
    total = users.count_documents({})
    left = users.count_documents({"left": True})
    banned = users.count_documents({"banned": True})
    active = users.count_documents({"left": {"$ne": True}, "banned": {"$ne": True}})
    prem = users.count_documents({"is_premium": True})
    await show(
        update,
        "👥 <b>Foydalanuvchilar bo'limi</b>\n\n"
        f"📊 Jami: {total} ta\n🟢 Faol: {active} ta\n🔴 Tark etgan: {left} ta\n"
        f"🚫 Bloklangan: {banned} ta\n💎 Premium: {prem} ta",
        Markup(
            [
                [Btn("📋 Foydalanuvchilar ro'yxati", callback_data="us:l:0")],
                [Btn("🔍 Foydalanuvchilarni qidirish", callback_data="us:s")],
                [Btn("⛔ Bloklangan foydalanuvchilar", callback_data="us:bl:0")],
                [Btn("◀️ Orqaga", callback_data="a:home")],
            ]
        ),
    )


def user_label(u):
    name = u.get("first_name") or "Noma'lum"
    return f"{name} · {u['_id']}"[:60]


async def v_user_list(update, page: int, banned: bool = False):
    per = 10
    flt = {"banned": True} if banned else {}
    total = users.count_documents(flt)
    pages = pages_of(total, per)
    page = max(0, min(page, pages - 1))
    docs = list(users.find(flt).sort("joined", -1).skip(page * per).limit(per))
    rows = [[Btn(user_label(u), callback_data=f"us:c:{u['_id']}")] for u in docs]
    rows.append(paginate_nav("us:bl" if banned else "us:l", page, pages))
    rows.append([Btn("◀️ Orqaga", callback_data="us:home")])
    title = "⛔ <b>Bloklangan foydalanuvchilar</b>" if banned else "📋 <b>Foydalanuvchilar ro'yxati</b>"
    await show(update, f"{title}\nJami: {total} ta", Markup(rows))


async def v_user_card(update, uid: int):
    u = users.find_one({"_id": uid})
    if not u:
        await show(update, "Foydalanuvchi topilmadi.", Markup([[Btn("◀️ Orqaga", callback_data="us:home")]]))
        return
    status = "🚫 Bloklangan" if u.get("banned") else ("🔴 Tark etgan" if u.get("left") else "🟢 Faol")
    uname = f"@{esc(u['username'])}" if u.get("username") else "yo'q"
    prem = "Ha" if u.get("is_premium") else "Yo'q"
    text = (
        "👤 <b>Foydalanuvchi</b>\n\n"
        f"ID: <code>{uid}</code>\nIsm: {esc(u.get('first_name'))}\nUsername: {uname}\n"
        f"Qo'shilgan: {fmt_dt(u.get('joined'))}\nOxirgi faollik: {fmt_dt(u.get('last_active'))}\n"
        f"Premium: {prem}\nHolat: {status}"
    )
    rows = []
    if not is_admin(uid):
        if u.get("banned"):
            rows.append([Btn("✅ Blokdan chiqarish", callback_data=f"us:unban:{uid}")])
        else:
            rows.append([Btn("🚫 Bloklash", callback_data=f"us:ban:{uid}")])
    rows.append([Btn("◀️ Orqaga", callback_data="us:l:0")])
    await show(update, text, Markup(rows))


async def cb_us(update, ctx, p):
    a = p[1]
    if a == "home":
        await v_users(update)
    elif a == "l":
        await v_user_list(update, int(p[2]))
    elif a == "bl":
        await v_user_list(update, int(p[2]), banned=True)
    elif a == "c":
        await v_user_card(update, int(p[2]))
    elif a == "s":
        ctx.user_data["st"] = {"k": "us_search"}
        await show(
            update,
            "🔍 ID, @username yoki ismni yozing:",
            Markup([[Btn("◀️ Orqaga", callback_data="us:home")]]),
        )
    elif a in ("ban", "unban"):
        uid = int(p[2])
        if not is_admin(uid):
            users.update_one({"_id": uid}, {"$set": {"banned": a == "ban"}})
        await v_user_card(update, uid)


@state("us_search")
async def s_us_search(update, ctx, st):
    t = (update.message.text or "").strip()
    if not t:
        return
    if re.fullmatch(r"\d+", t):
        flt = {"_id": int(t)}
    elif t.startswith("@"):
        flt = {"username": {"$regex": f"^{re.escape(t[1:])}$", "$options": "i"}}
    else:
        flt = {"first_name": {"$regex": re.escape(t), "$options": "i"}}
    found = list(users.find(flt).limit(20))
    ctx.user_data.pop("st", None)
    if not found:
        await update.message.reply_text("Hech kim topilmadi.")
        await v_users(update)
        return
    rows = [[Btn(user_label(u), callback_data=f"us:c:{u['_id']}")] for u in found]
    rows.append([Btn("◀️ Orqaga", callback_data="us:home")])
    await update.message.reply_text("Topilganlar:", reply_markup=Markup(rows))


# ---------- Dizayn ----------
async def v_design(update):
    await show(
        update,
        "🎨 <b>Dizayn bo'limi</b>\n\nBot foydalanuvchilarga qanday ko'rinishini shu yerda sozlaysiz.",
        Markup(
            [
                [Btn("📝 Matnlar", callback_data="ds:t"), Btn("🔘 Tugmalar", callback_data="ds:b")],
                [Btn("🔐 Majburiy obuna", callback_data="ds:e:sub_text"), Btn("👋 Start xabari", callback_data="ds:start")],
                [Btn("👤 Foydalanuvchi menyusi", callback_data="ds:m")],
                [Btn("🛠 Admin panel", callback_data="a:home")],
            ]
        ),
    )


async def v_item_list(update, kind: str):
    rows = [[Btn(v[0], callback_data=f"ds:e:{k}")] for k, v in ITEMS.items() if v[3] == kind]
    rows.append([Btn("◀️ Orqaga", callback_data="ds:home")])
    title = "📝 <b>Matnlar</b>" if kind == "t" else "🔘 <b>Tugmalar</b>"
    await show(update, f"{title}\n\nQaysi birini o'zgartiramiz?", Markup(rows))


async def v_item(update, key: str):
    title, _, is_html, kind = ITEMS[key]
    cur = T(key)
    shown = cur if is_html else (esc(cur) if cur else "<i>(bo'sh)</i>")
    back = {"t": "ds:t", "b": "ds:b", "s": "ds:home"}[kind]
    await show(
        update,
        f"📝 <b>{title}</b>\n\nHozirgi holat:\n{shown}",
        Markup(
            [
                [Btn("✏️ O'zgartirish", callback_data=f"ds:i:{key}")],
                [Btn("♻️ Asl holiga qaytarish", callback_data=f"ds:r:{key}")],
                [Btn("◀️ Orqaga", callback_data=back)],
            ]
        ),
    )


async def v_start_design(update):
    st = S["start"]
    kind = {"photo": "rasm + matn", "video": "video + matn"}.get(st.get("type"), "matn")
    cur = st.get("html") or esc(DEFAULT_START)
    await show(
        update,
        f"👋 <b>Start xabari</b> ({kind})\n\nHozirgi holat:\n{cur}",
        Markup(
            [
                [Btn("✏️ O'zgartirish", callback_data="ds:starti")],
                [Btn("♻️ Asl holiga qaytarish", callback_data="ds:startr")],
                [Btn("◀️ Orqaga", callback_data="ds:home")],
            ]
        ),
    )


async def v_user_menu(update):
    items = S["user_menu"]
    lst = "\n".join(f"{i}. {esc(b['label'])}" for i, b in enumerate(items, 1)) or "<i>(bo'sh)</i>"
    await show(
        update,
        "👤 <b>Foydalanuvchi menyusi</b>\n\nFoydalanuvchilarda pastda chiqadigan tugmalar. "
        "Tugma bosilganda bot siz belgilagan matnni yuboradi.\n\n"
        f"Hozir:\n{lst}\n\n<i>O'zgarish foydalanuvchida /start bosganda yangilanadi.</i>",
        Markup(
            [
                [Btn("➕ Tugma qo'shish", callback_data="ds:ma")],
                [Btn("🗑 Tugmani o'chirish", callback_data="ds:md")],
                [Btn("◀️ Orqaga", callback_data="ds:home")],
            ]
        ),
    )


async def cb_ds(update, ctx, p):
    a = p[1]
    if a == "home":
        await v_design(update)
    elif a in ("t", "b"):
        await v_item_list(update, a)
    elif a == "e":
        await v_item(update, p[2])
    elif a == "i":
        key = p[2]
        ctx.user_data["st"] = {"k": "item", "key": key}
        hint = "\n\nQalin, kursiv va havola formatlari saqlanadi." if ITEMS[key][2] else ""
        await show(
            update,
            f"✏️ <b>{ITEMS[key][0]}</b>\n\nYangi matnni yuboring.{hint}",
            Markup([[Btn("◀️ Orqaga", callback_data=f"ds:e:{key}")]]),
        )
    elif a == "r":
        S["texts"].pop(p[2], None)
        save_settings()
        await v_item(update, p[2])
    elif a == "start":
        await v_start_design(update)
    elif a == "starti":
        ctx.user_data["st"] = {"k": "start_in"}
        await show(
            update,
            "✏️ Yangi start xabarini yuboring.\n\nMatn yoki rasm/video (izoh bilan) yuborishingiz mumkin. "
            "Qalin, kursiv va havola formatlari saqlanadi.",
            Markup([[Btn("◀️ Orqaga", callback_data="ds:start")]]),
        )
    elif a == "startr":
        S["start"] = {}
        save_settings()
        await v_start_design(update)
    elif a == "m":
        await v_user_menu(update)
    elif a == "ma":
        ctx.user_data["st"] = {"k": "um_label"}
        await show(update, "➕ Tugma nomini yozing:", Markup([[Btn("◀️ Orqaga", callback_data="ds:m")]]))
    elif a == "md":
        items = S["user_menu"]
        if not items:
            await v_user_menu(update)
            return
        rows = [[Btn(f"🗑 {b['label']}"[:60], callback_data=f"ds:mx:{i}")] for i, b in enumerate(items)]
        rows.append([Btn("◀️ Orqaga", callback_data="ds:m")])
        await show(update, "Qaysi tugmani o'chiramiz?", Markup(rows))
    elif a == "mx":
        i = int(p[2])
        if 0 <= i < len(S["user_menu"]):
            S["user_menu"].pop(i)
            save_settings()
        await v_user_menu(update)


@state("item")
async def s_item(update, ctx, st):
    m = update.message
    if not m.text:
        await m.reply_text("Faqat matn yuboring.")
        return
    key = st["key"]
    S["texts"][key] = m.text_html if ITEMS[key][2] else m.text.strip()
    save_settings()
    ctx.user_data.pop("st", None)
    await m.reply_text("✅ Saqlandi.")
    await v_item(update, key)


@state("start_in")
async def s_start_in(update, ctx, st):
    m = update.message
    if m.photo:
        new = {"type": "photo", "file_id": m.photo[-1].file_id, "html": m.caption_html or ""}
    elif m.video:
        new = {"type": "video", "file_id": m.video.file_id, "html": m.caption_html or ""}
    elif m.text:
        new = {"type": "text", "html": m.text_html}
    else:
        await m.reply_text("Matn, rasm yoki video yuboring.")
        return
    S["start"] = new
    save_settings()
    ctx.user_data.pop("st", None)
    await m.reply_text("✅ Saqlandi.")
    await v_start_design(update)


@state("um_label")
async def s_um_label(update, ctx, st):
    t = (update.message.text or "").strip()
    if not t or len(t) > 40:
        await update.message.reply_text("Tugma nomi 1 dan 40 gacha belgi bo'lsin.")
        return
    ctx.user_data["st"] = {"k": "um_text", "label": t}
    await update.message.reply_text("Tugma bosilganda bot qaysi matnni yuborsin? Matnni yozing:")


@state("um_text")
async def s_um_text(update, ctx, st):
    m = update.message
    if not m.text:
        await m.reply_text("Faqat matn yuboring.")
        return
    S["user_menu"].append({"label": st["label"], "text": m.text_html})
    save_settings()
    ctx.user_data.pop("st", None)
    await m.reply_text("✅ Tugma qo'shildi.")
    await v_user_menu(update)


# ---------- Sozlamalar ----------
async def v_settings(update):
    rows = [[Btn("↗️ Kontentni himoya qilish", callback_data="pr:home")]]
    if update.effective_user.id == OWNER_ID:
        rows.append([Btn("👮‍♀️ Adminlar", callback_data="ad:home")])
    rows.append([Btn("📬 Postlar", callback_data="po:home")])
    rows.append([Btn("◀️ Orqaga", callback_data="a:home")])
    await show(update, "⚙️ <b>Sozlamalar</b>", Markup(rows))


def lock(on: bool) -> str:
    return "🔒 Taqiqlangan" if on else "🔓 Ruxsat"


async def v_protect(update):
    r, pr = S["protect_regular"], S["protect_premium"]
    await show(
        update,
        "↗️ <b>Kontentni himoya qilish sozlamalari</b>\n\n"
        "Ushbu bo'lim orqali foydalanuvchilar kinolarni boshqalarga yuborishi (Forward) yoki saqlab olishini "
        "nazorat qilasiz.\n\n"
        f"👥 <b>Oddiy foydalanuvchilar:</b>\n└ Holat: {lock(r)}\n\n"
        f"🌟 <b>Premium foydalanuvchilar:</b>\n└ Holat: {lock(pr)}\n\n"
        "Tugmani bosish orqali ruxsatni o'zgartirishingiz mumkin:",
        Markup(
            [
                [Btn(f"👥 Oddiy ({'🔓 Ruxsat berish' if r else '🔒 Taqiqlash'})", callback_data="pr:u")],
                [Btn(f"🌟 Premium ({'🔓 Ruxsat berish' if pr else '🔒 Taqiqlash'})", callback_data="pr:p")],
                [Btn("◀️ Orqaga", callback_data="se:home")],
            ]
        ),
    )


async def cb_pr(update, ctx, p):
    if p[1] == "u":
        S["protect_regular"] = not S["protect_regular"]
        save_settings()
    elif p[1] == "p":
        S["protect_premium"] = not S["protect_premium"]
        save_settings()
    await v_protect(update)


# ---------- Adminlar (faqat asosiy admin) ----------
async def v_admins(update):
    await show(
        update,
        "👮‍♀️ <b>Adminlar bo'limidasiz:</b>\n\n🔹 Bu yerda yangi admin qo'shishingiz yoki mavjudlarini "
        "boshqarishingiz mumkin.",
        Markup(
            [
                [Btn("➕ Admin qo'shish", callback_data="ad:add"), Btn("➖ Admin o'chirish", callback_data="ad:rm")],
                [Btn("📋 Adminlar ro'yxati", callback_data="ad:list")],
                [Btn("⚙️ Sozlamalar", callback_data="se:home")],
            ]
        ),
    )


def admin_name(uid: int) -> str:
    u = users.find_one({"_id": uid}) or {}
    name = u.get("first_name") or "Noma'lum"
    return f"{name}" + (f" (@{u['username']})" if u.get("username") else "")


async def cb_ad(update, ctx, p):
    if update.effective_user.id != OWNER_ID:
        return
    a = p[1]
    back = Markup([[Btn("◀️ Orqaga", callback_data="ad:home")]])
    if a == "home":
        await v_admins(update)
    elif a == "add":
        ctx.user_data["st"] = {"k": "ad_add"}
        await show(
            update,
            "➕ Yangi adminning Telegram ID sini yozing yoki uning xabarini shu yerga forward qiling.\n\n"
            "<i>U botga kamida bir marta /start bosgan bo'lishi kerak.</i>",
            back,
        )
    elif a == "list":
        lines = [f"👑 {esc(admin_name(OWNER_ID))} — <code>{OWNER_ID}</code> (asosiy admin)"]
        for ad in admins.find():
            lines.append(f"👮 {esc(admin_name(ad['_id']))} — <code>{ad['_id']}</code>")
        await show(update, "📋 <b>Adminlar ro'yxati</b>\n\n" + "\n".join(lines), back)
    elif a == "rm":
        docs = list(admins.find())
        if not docs:
            await show(update, "O'chirish uchun qo'shimcha admin yo'q.", back)
            return
        rows = [[Btn(f"➖ {admin_name(d['_id'])}"[:60], callback_data=f"ad:rc:{d['_id']}")] for d in docs]
        rows.append([Btn("◀️ Orqaga", callback_data="ad:home")])
        await show(update, "Qaysi adminni o'chiramiz?", Markup(rows))
    elif a == "rc":
        await show(
            update,
            f"<b>{esc(admin_name(int(p[2])))}</b> ni adminlikdan olib tashlaysizmi?",
            Markup(
                [
                    [Btn("✅ Ha", callback_data=f"ad:ry:{p[2]}")],
                    [Btn("◀️ Yo'q", callback_data="ad:rm")],
                ]
            ),
        )
    elif a == "ry":
        uid = int(p[2])
        if uid != OWNER_ID:
            admins.delete_one({"_id": uid})
            ADMIN_IDS.discard(uid)
            try:
                await ctx.bot.send_message(uid, "Siz adminlikdan olib tashlandingiz.", reply_markup=ReplyKeyboardRemove())
            except TelegramError:
                pass
        await v_admins(update)


@state("ad_add")
async def s_ad_add(update, ctx, st):
    m = update.message
    uid = None
    su = getattr(m.forward_origin, "sender_user", None) if m.forward_origin else None
    if su:
        uid = su.id
    elif m.text and re.fullmatch(r"\d+", m.text.strip()):
        uid = int(m.text.strip())
    if uid is None:
        await m.reply_text("❌ ID yozing yoki foydalanuvchining xabarini forward qiling.")
        return
    if not users.find_one({"_id": uid}):
        await m.reply_text("❌ Bu foydalanuvchi botga hali /start bosmagan.")
        return
    if is_admin(uid):
        await m.reply_text("Bu foydalanuvchi allaqachon admin.")
        return
    admins.insert_one({"_id": uid, "added": now()})
    ADMIN_IDS.add(uid)
    ctx.user_data.pop("st", None)
    try:
        await ctx.bot.send_message(uid, "Siz admin qilindingiz. Panelni ochish uchun /admin ni bosing.")
    except TelegramError:
        pass
    await m.reply_text(f"✅ Admin qo'shildi: {admin_name(uid)}")
    await v_admins(update)


# ---------- Postlar (kanalga tugmali post) ----------
async def v_posts(update):
    await show(
        update,
        "📬 <b>Postlar bo'limi</b>\n\nKanal yoki guruhga post yuborish uchun quyidagi tugmani bosing.",
        Markup([[Btn("📝 Yangi post yaratish", callback_data="po:new")], [Btn("◀️ Orqaga", callback_data="se:home")]]),
    )


def post_markup(bot, code):
    if not code:
        return None
    return Markup([[Btn(T("watch"), url=f"https://t.me/{bot.username}?start={code}")]])


async def po_ask_target(update, ctx):
    ctx.user_data["st"] = {"k": "po_target"}
    rows = [[Btn(f"📢 {c['title']}"[:60], callback_data=f"po:to:{c['_id']}")] for c in channels.find()]
    rows.append([Btn("◀️ Orqaga", callback_data="po:new")])
    await show(
        update,
        "Qayerga yuboramiz?\n\nKanal yoki guruhning @username ini yozing, ID sini yuboring, forward qiling "
        "yoki pastdan tanlang. Bot u yerda admin bo'lishi kerak.",
        Markup(rows),
    )


async def po_preview(update, ctx):
    fl = ctx.user_data["fl"]
    ctx.user_data.pop("st", None)
    chat = update.effective_chat
    await ctx.bot.copy_message(chat.id, fl["cid"], fl["mid"], reply_markup=post_markup(ctx.bot, fl.get("code")))
    await chat.send_message(
        "Post shunday ko'rinishda yuboriladi. Tasdiqlaysizmi?",
        reply_markup=Markup(
            [
                [Btn("✅ Yuborish", callback_data="po:go"), Btn("❌ Bekor qilish", callback_data="po:cancel")],
                [Btn("◀️ Orqaga", callback_data="po:new")],
            ]
        ),
    )


async def cb_po(update, ctx, p):
    a = p[1]
    fl = ctx.user_data.get("fl")
    if a == "home":
        await v_posts(update)
    elif a == "new":
        ctx.user_data["fl"] = {}
        ctx.user_data["st"] = {"k": "po_msg"}
        await show(
            update,
            "📝 <b>Yangi post</b>\n\nPost xabarini yuboring (rasm yoki video bilan matn). "
            "Qalin, kursiv va havola formatlari saqlanadi.",
            Markup([[Btn("◀️ Orqaga", callback_data="po:home")]]),
        )
    elif a == "skip" and fl is not None:
        fl["code"] = None
        await po_ask_target(update, ctx)
    elif a == "pick" and fl is not None:
        fl["code"] = int(p[2])
        await po_ask_target(update, ctx)
    elif a == "to" and fl is not None:
        fl["to"] = int(p[2])
        await po_preview(update, ctx)
    elif a == "go" and fl is not None:
        try:
            await ctx.bot.copy_message(
                fl["to"], fl["cid"], fl["mid"], reply_markup=post_markup(ctx.bot, fl.get("code"))
            )
            await show(update, "✅ Post yuborildi.", Markup([[Btn("◀️ Orqaga", callback_data="po:home")]]))
        except TelegramError as e:
            await show(
                update,
                f"❌ Yuborib bo'lmadi: {esc(e)}",
                Markup([[Btn("◀️ Orqaga", callback_data="po:home")]]),
            )
        ctx.user_data.pop("fl", None)
    elif a == "cancel":
        ctx.user_data.pop("fl", None)
        await v_posts(update)


@state("po_msg")
async def s_po_msg(update, ctx, st):
    m = update.message
    ctx.user_data["fl"].update(mid=m.message_id, cid=m.chat_id)
    ctx.user_data["st"] = {"k": "po_movie"}
    await m.reply_text(
        "Postga «Tomosha Qilish» tugmasi qo'shamizmi? Kino kodini yoki nomini yozing.",
        reply_markup=Markup(
            [
                [Btn("⏭ Tugmasiz", callback_data="po:skip")],
                [Btn("◀️ Orqaga", callback_data="po:new")],
            ]
        ),
    )


@state("po_movie")
async def s_po_movie(update, ctx, st):
    t = (update.message.text or "").strip()
    if not t:
        return
    if t.isdigit():
        m = movies.find_one({"code": int(t)})
        if not m:
            await update.message.reply_text("❌ Bu kod bo'yicha kino topilmadi.")
            return
        ctx.user_data["fl"]["code"] = m["code"]
        await po_ask_target(update, ctx)
        return
    q = t.casefold()
    found = [m for m in movies.find().sort("code", 1) if q in m["title"].casefold()][:20]
    if not found:
        await update.message.reply_text("❌ Hech narsa topilmadi. Boshqa nom yoki kod yozing.")
        return
    rows = [[Btn(f"{m['code']}. {m['title']}"[:60], callback_data=f"po:pick:{m['code']}")] for m in found]
    rows.append([Btn("◀️ Orqaga", callback_data="po:new")])
    await update.message.reply_text("Qaysi kino?", reply_markup=Markup(rows))


@state("po_target")
async def s_po_target(update, ctx, st):
    chat = await resolve_chat(update, ctx)
    if not chat:
        return
    ctx.user_data["fl"]["to"] = chat.id
    await po_preview(update, ctx)


# ============================== TUGMALAR (CALLBACK) YO'NALTIRGICHI ==============================
async def cb_a(update, ctx, p):
    if p[1] == "home":
        await v_panel(update)
    elif p[1] == "close":
        try:
            await update.callback_query.message.delete()
        except TelegramError:
            pass


async def cb_st(update, ctx, p):
    await v_stats(update, ctx)


async def cb_se(update, ctx, p):
    await v_settings(update)


ROUTES = {
    "a": cb_a, "mv": cb_mv, "st": cb_st, "ch": cb_ch, "bc": cb_bc, "us": cb_us,
    "ds": cb_ds, "se": cb_se, "pr": cb_pr, "ad": cb_ad, "po": cb_po,
}
MIDFLOW = {"bc:skip", "bc:go", "po:skip", "po:go"}


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data or ""
    uid = q.from_user.id
    if d == "noop":
        await safe_answer(q)
        return
    doc = touch_user(update)
    if doc and doc.get("banned") and not is_admin(uid):
        await safe_answer(q, "Siz bloklangansiz.", alert=True)
        return

    # --- oddiy foydalanuvchi tugmalari ---
    if d.startswith("m:"):
        await safe_answer(q)
        code = int(d[2:])
        if await gate(update, ctx, code):
            await deliver(ctx.bot, q.message.chat_id, uid, code, bool(doc.get("is_premium")))
        return
    if d.startswith("chk:"):
        code = int(d[4:] or 0)
        missing = [] if is_admin(uid) else await not_subscribed(ctx.bot, uid)
        if missing:
            await safe_answer(q, "Siz hali barcha kanallarga obuna bo'lmagansiz!", alert=True)
            return
        await safe_answer(q)
        try:
            await q.message.delete()
        except TelegramError:
            pass
        if code:
            await deliver(ctx.bot, q.message.chat_id, uid, code, bool(doc.get("is_premium")))
        else:
            await send_start(ctx.bot, q.message.chat_id, uid)
        return

    # --- admin tugmalari ---
    if not is_admin(uid):
        await safe_answer(q)
        return
    await safe_answer(q)
    ctx.user_data.pop("st", None)
    if not (d in MIDFLOW or d.startswith(("po:pick:", "po:to:"))):
        ctx.user_data.pop("fl", None)
    p = d.split(":")
    fn = ROUTES.get(p[0])
    if fn:
        await fn(update, ctx, p)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Xatolik: %s", ctx.error, exc_info=ctx.error)


async def post_init(app):
    await app.bot.set_my_commands([("start", "Botni ishga tushirish")])
    log.info("Bot ishga tushdi: @%s", app.bot.username)


def main():
    users.create_index("joined")
    users.create_index("last_active")
    movies.create_index("code", unique=True)
    downloads.create_index("at")

    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", on_start, filters=private))
    app.add_handler(CommandHandler("admin", on_admin_cmd, filters=private))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, on_channel_post))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, on_private))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(ChatMemberHandler(on_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
