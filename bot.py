# -*- coding: utf-8 -*-
"""
Бот недвижимости в Болгарии: русский + иврит.
- Клиенты: выбор языка, просмотр канала, подбор квартиры, вопрос менеджеру, заявка по объекту.
- Админ: /add — добавить объект (публикуется сразу в русский и ивритский каналы),
         /sold N, /active N, /delete N, /list, /cancel.
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
from collections import defaultdict
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from texts import CHOOSE_LANG, CITIES, DEALS, LANG_BUTTONS, POST, T, TYPES

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in re.findall(r"-?\d+", os.getenv("ADMIN_IDS", ""))}
CHANNELS = {"ru": os.getenv("CHANNEL_RU", "").strip(), "he": os.getenv("CHANNEL_HE", "").strip()}
DB_PATH = os.getenv("DB_PATH", "bot.db")
MAX_PHOTOS = 10
BOT_USERNAME = ""  # заполняется при запуске

router = Router()
IS_ADMIN = F.from_user.id.in_(ADMIN_IDS)

# ======================= База данных =======================
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, lang TEXT, name TEXT, username TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS objects(
        id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, status TEXT DEFAULT 'active',
        created TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS posts(
        object_id INTEGER, chat TEXT, lang TEXT, message_ids TEXT, main_msg INTEGER, kind TEXT);
    CREATE TABLE IF NOT EXISTS leads(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, data TEXT,
        created TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS relay(
        admin_chat INTEGER, admin_msg INTEGER, user_id INTEGER,
        PRIMARY KEY(admin_chat, admin_msg));
    """
)
db.commit()


def save_user(u: User, lang: str | None = None) -> None:
    db.execute(
        "INSERT INTO users(id, lang, name, username) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username, "
        "lang=COALESCE(excluded.lang, users.lang)",
        (u.id, lang, u.full_name, u.username),
    )
    db.commit()


def get_lang(uid: int) -> str | None:
    r = db.execute("SELECT lang FROM users WHERE id=?", (uid,)).fetchone()
    return r["lang"] if r and r["lang"] in T else None


def get_object(oid: int):
    r = db.execute("SELECT * FROM objects WHERE id=?", (oid,)).fetchone()
    return (json.loads(r["data"]), r["status"]) if r else (None, None)


# ======================= Помощники =======================
def name_of(items, key, lang):
    for k, ru, he in items:
        if k == key:
            return ru if lang == "ru" else he
    return ""


def tagify(s: str) -> str:
    s = re.sub(r"[^\w\s]", "", s).strip()
    return "#" + re.sub(r"\s+", "_", s)


def fmt_num(x) -> str:
    x = float(x)
    s = f"{x:,.0f}" if x == int(x) else f"{x:,.1f}"
    return s.replace(",", " ")


def parse_num(text: str):
    t = text.strip().replace("€", "").replace(" ", "").replace("\u00a0", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", t):
        t = t.replace(",", "")
    else:
        t = t.replace(",", ".")
    try:
        v = float(t)
        return v if v >= 0 else None
    except ValueError:
        return None


def user_link(u: User) -> str:
    s = f'<a href="tg://user?id={u.id}">{escape(u.full_name)}</a>'
    return s + (f" (@{u.username})" if u.username else "")


def channel_url(chat: str) -> str | None:
    return f"https://t.me/{chat[1:]}" if chat.startswith("@") else None


def build_post(oid, d: dict, lang: str, status: str | None = None, link: bool = True) -> str:
    L = POST[lang]
    lines = []
    if status:
        lines.append(f"<b>{L[status]}</b>\n")
    lines.append(f"🏠 <b>{name_of(TYPES, d['ptype'], lang)} · {name_of(DEALS, d['deal'], lang)}</b>")
    lines.append(f"📍 {name_of(CITIES, d['city'], lang)}")
    if d.get("area"):
        lines.append(f"📐 {L['area']}: {fmt_num(d['area'])} {L['m2']}")
    if d.get("floor"):
        lines.append(f"🏢 {L['floor']}: {escape(d['floor'])}")
    if d.get("sea"):
        lines.append(f"🌊 {L['sea']}: {fmt_num(d['sea'])} {L['m']}")
    unit = L["per_month"] if d["deal"] == "rent" else ""
    lines.append(f"💶 <b>{L['price']}: {fmt_num(d['price'])} €{unit}</b>")
    if d.get("fee"):
        lines.append(f"🧾 {L['fee']}: {fmt_num(d['fee'])} €{L['per_year']}")
    desc = d.get(f"desc_{lang}")
    if desc:
        lines.append("\n" + escape(desc))
    tags = [tagify(name_of(DEALS, d["deal"], lang)), tagify(name_of(TYPES, d["ptype"], lang))]
    if d["city"] != "other":
        tags.insert(0, tagify(name_of(CITIES, d["city"], lang)))
    lines.append("\n" + " ".join(tags))
    if link and BOT_USERNAME and oid:
        lines.append(f'\n<a href="https://t.me/{BOT_USERNAME}?start=obj_{oid}">{L["more"]}</a>')
    if oid:
        lines.append(f"🆔 {L['num']}{oid}")
    return "\n".join(lines)


# ======================= Клавиатуры =======================
def main_kb(lang: str) -> ReplyKeyboardMarkup:
    t = T[lang]
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t["btn_browse"])],
            [KeyboardButton(text=t["btn_pick"]), KeyboardButton(text=t["btn_contact"])],
            [KeyboardButton(text=t["btn_lang"])],
        ],
        resize_keyboard=True,
    )


def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=n, callback_data=f"lang:{k}") for k, n in LANG_BUTTONS]]
    )


def options_kb(prefix: str, items, lang: str, any_text: str | None = None, cols: int = 2):
    b = InlineKeyboardBuilder()
    for k, ru, he in items:
        b.button(text=ru if lang == "ru" else he, callback_data=f"{prefix}:{k}")
    if any_text:
        b.button(text=any_text, callback_data=f"{prefix}:any")
    b.adjust(cols)
    return b.as_markup()


def phone_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T[lang]["btn_send_phone"], request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def btn_texts(key: str) -> set[str]:
    return {T[l][key] for l in T}


# ======================= Состояния =======================
class Pick(StatesGroup):
    city = State()
    ptype = State()
    budget = State()


class Lead(StatesGroup):
    contact = State()


class Ask(StatesGroup):
    question = State()


class Add(StatesGroup):
    deal = State()
    city = State()
    ptype = State()
    area = State()
    floor = State()
    sea = State()
    price = State()
    fee = State()
    desc_ru = State()
    desc_he = State()
    photos = State()
    confirm = State()


# ======================= Уведомления админам =======================
async def notify_admins(bot: Bot, text: str, user_id: int | None = None) -> None:
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст — заявка не доставлена: %s", text)
    for aid in ADMIN_IDS:
        try:
            m = await bot.send_message(aid, text)
            if user_id:
                db.execute("INSERT OR REPLACE INTO relay VALUES(?,?,?)", (aid, m.message_id, user_id))
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось отправить админу %s: %s", aid, e)
    db.commit()


# ======================= Общие команды =======================
@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"Ваш ID: <code>{m.from_user.id}</code>")


async def send_welcome(m: Message, lang: str):
    await m.answer(T[lang]["welcome"], reply_markup=main_kb(lang))


async def start_interest(m: Message, state: FSMContext, lang: str, oid: int):
    d, status = get_object(oid)
    t = T[lang]
    if d is None:
        await m.answer(t["obj_not_found"])
    elif status != "active":
        await m.answer(t["obj_closed"])
    else:
        await m.answer(t["obj_interest"] + "\n\n" + build_post(oid, d, lang, link=False))
    await state.set_state(Lead.contact)
    await state.update_data(kind="object", obj_id=oid)
    await m.answer(t["ask_contact"], reply_markup=phone_kb(lang))


@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    save_user(m.from_user)
    oid = None
    if command.args and re.fullmatch(r"obj_\d+", command.args):
        oid = int(command.args[4:])
    lang = get_lang(m.from_user.id)
    if not lang:
        await state.update_data(pending_obj=oid)
        await m.answer(CHOOSE_LANG, reply_markup=lang_kb())
        return
    if oid:
        await start_interest(m, state, lang, oid)
    else:
        await send_welcome(m, lang)


@router.callback_query(F.data.startswith("lang:"))
async def cb_lang(c: CallbackQuery, state: FSMContext):
    lang = c.data.split(":", 1)[1]
    if lang not in T:
        return await c.answer()
    save_user(c.from_user, lang)
    oid = (await state.get_data()).get("pending_obj")
    await state.clear()
    await c.message.edit_text(T[lang]["lang_set"])
    if oid:
        await start_interest(c.message, state, lang, oid)
    else:
        await send_welcome(c.message, lang)
    await c.answer()


# ======================= Админ: ответ клиенту через reply =======================
def is_relay_reply(m: Message) -> bool:
    if not m.reply_to_message or m.from_user.id not in ADMIN_IDS:
        return False
    r = db.execute(
        "SELECT 1 FROM relay WHERE admin_chat=? AND admin_msg=?", (m.chat.id, m.reply_to_message.message_id)
    ).fetchone()
    return r is not None


@router.message(is_relay_reply)
async def admin_reply(m: Message):
    r = db.execute(
        "SELECT user_id FROM relay WHERE admin_chat=? AND admin_msg=?", (m.chat.id, m.reply_to_message.message_id)
    ).fetchone()
    uid = r["user_id"]
    lang = get_lang(uid) or "ru"
    try:
        await m.bot.send_message(uid, T[lang]["manager_reply"])
        await m.bot.copy_message(chat_id=uid, from_chat_id=m.chat.id, message_id=m.message_id)
        await m.reply("✅ Ответ отправлен клиенту")
    except Exception as e:  # noqa: BLE001
        await m.reply(f"⚠️ Не удалось отправить: {escape(str(e))}")


# ======================= Админ: команды =======================
ADMIN_HELP = (
    "<b>Команды админа</b>\n"
    "/add — добавить объект\n"
    "/list — последние объекты\n"
    "/sold N — отметить «продано/сдано»\n"
    "/active N — вернуть в продажу\n"
    "/delete N — удалить объявление из каналов\n"
    "/cancel — отменить текущее действие\n\n"
    "Чтобы ответить клиенту, ответьте (Reply) на его заявку или вопрос."
)


@router.message(Command("admin", "help"), IS_ADMIN)
async def cmd_admin(m: Message):
    await m.answer(ADMIN_HELP)


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    PHOTOS.pop(m.from_user.id, None)
    lang = get_lang(m.from_user.id) or "ru"
    await m.answer("Отменено.", reply_markup=main_kb(lang))


@router.message(Command("list"), IS_ADMIN)
async def cmd_list(m: Message):
    rows = db.execute("SELECT * FROM objects ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        return await m.answer("Объектов пока нет. Добавьте первый: /add")
    icons = {"active": "🟢", "sold": "🔴", "rented": "🔴", "deleted": "⚫"}
    out = []
    for r in rows:
        d = json.loads(r["data"])
        out.append(
            f"{icons.get(r['status'], '•')} №{r['id']} · {name_of(CITIES, d['city'], 'ru')} · "
            f"{name_of(TYPES, d['ptype'], 'ru')} · {fmt_num(d['price'])} €"
        )
    await m.answer("\n".join(out))


async def set_status(bot: Bot, oid: int, status: str | None) -> str:
    d, cur = get_object(oid)
    if d is None:
        return "Объект не найден."
    new = status or "active"
    db.execute("UPDATE objects SET status=? WHERE id=?", (new, oid))
    db.commit()
    errors = []
    for p in db.execute("SELECT * FROM posts WHERE object_id=?", (oid,)).fetchall():
        text = build_post(oid, d, p["lang"], status=status, link=status is None)
        try:
            if p["kind"] == "caption":
                await bot.edit_message_caption(chat_id=p["chat"], message_id=p["main_msg"], caption=text)
            else:
                await bot.edit_message_text(text=text, chat_id=p["chat"], message_id=p["main_msg"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{p['chat']}: {e}")
    return "Готово ✅" + ("\n⚠️ " + "\n".join(map(str, errors)) if errors else "")


@router.message(Command("sold"), IS_ADMIN)
async def cmd_sold(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Использование: /sold 12")
    oid = int(command.args)
    d, _ = get_object(oid)
    if d is None:
        return await m.answer("Объект не найден.")
    await m.answer(await set_status(m.bot, oid, "rented" if d["deal"] == "rent" else "sold"))


@router.message(Command("active"), IS_ADMIN)
async def cmd_active(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Использование: /active 12")
    await m.answer(await set_status(m.bot, int(command.args), None))


@router.message(Command("delete"), IS_ADMIN)
async def cmd_delete(m: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Использование: /delete 12")
    oid = int(command.args)
    posts = db.execute("SELECT * FROM posts WHERE object_id=?", (oid,)).fetchall()
    for p in posts:
        for mid in json.loads(p["message_ids"]):
            try:
                await m.bot.delete_message(p["chat"], mid)
            except Exception as e:  # noqa: BLE001
                log.warning("delete %s/%s: %s", p["chat"], mid, e)
    db.execute("UPDATE objects SET status='deleted' WHERE id=?", (oid,))
    db.execute("DELETE FROM posts WHERE object_id=?", (oid,))
    db.commit()
    await m.answer(f"Объект №{oid} удалён из каналов 🗑")


# ======================= Админ: добавление объекта =======================
PHOTOS: dict[int, list[str]] = defaultdict(list)

# (состояние, поле, тип, вопрос)
ADD_FLOW = [
    (Add.area, "area", "num", "📐 Площадь, м² (число):"),
    (Add.floor, "floor", "text_opt", "🏢 Этаж, например 3/6 (или «-», чтобы пропустить):"),
    (Add.sea, "sea", "num_opt", "🌊 Расстояние до моря в метрах (или «-»):"),
    (Add.price, "price", "num", "💶 Цена в евро (для аренды — в месяц):"),
    (Add.fee, "fee", "num_opt", "🧾 Такса поддержки комплекса, € в год (или «-»):"),
    (Add.desc_ru, "desc_ru", "text_opt", "📝 Описание на русском (или «-»):"),
    (Add.desc_he, "desc_he", "text_opt", "📝 Описание на иврите (или «-», тогда в ивритском канале будет без описания):"),
]


@router.message(Command("add"), IS_ADMIN)
async def cmd_add(m: Message, state: FSMContext):
    if not any(CHANNELS.values()):
        return await m.answer("⚠️ Не указаны каналы (CHANNEL_RU / CHANNEL_HE в файле .env).")
    await state.clear()
    await state.set_state(Add.deal)
    await m.answer("Новый объект. Отменить можно командой /cancel", reply_markup=ReplyKeyboardRemove())
    await m.answer("Тип сделки:", reply_markup=options_kb("ad", DEALS, "ru"))


@router.callback_query(Add.deal, F.data.startswith("ad:"))
async def add_deal(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    await state.update_data(deal=key)
    await c.message.edit_text(f"Тип сделки: <b>{name_of(DEALS, key, 'ru')}</b>")
    await state.set_state(Add.city)
    await c.message.answer("Город:", reply_markup=options_kb("ac", CITIES, "ru"))
    await c.answer()


@router.callback_query(Add.city, F.data.startswith("ac:"))
async def add_city(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    await state.update_data(city=key)
    await c.message.edit_text(f"Город: <b>{name_of(CITIES, key, 'ru')}</b>")
    await state.set_state(Add.ptype)
    await c.message.answer("Тип жилья:", reply_markup=options_kb("at", TYPES, "ru"))
    await c.answer()


@router.callback_query(Add.ptype, F.data.startswith("at:"))
async def add_type(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    await state.update_data(ptype=key)
    await c.message.edit_text(f"Тип жилья: <b>{name_of(TYPES, key, 'ru')}</b>")
    await state.set_state(ADD_FLOW[0][0])
    await c.message.answer(ADD_FLOW[0][3])
    await c.answer()


@router.message(StateFilter(*[s for s, *_ in ADD_FLOW]), F.text, IS_ADMIN)
async def add_text_step(m: Message, state: FSMContext):
    cur = await state.get_state()
    idx = next(i for i, (s, *_) in enumerate(ADD_FLOW) if s.state == cur)
    _, key, kind, _ = ADD_FLOW[idx]
    text = m.text.strip()
    if kind.endswith("_opt") and text == "-":
        value = None
    elif kind.startswith("num"):
        value = parse_num(text)
        if value is None:
            return await m.answer("Нужно число, например 65 или 85 000. Попробуйте ещё раз:")
    else:
        value = text[:2500]
    await state.update_data(**{key: value})

    if idx + 1 < len(ADD_FLOW):
        nxt = ADD_FLOW[idx + 1]
        await state.set_state(nxt[0])
        await m.answer(nxt[3])
    else:
        PHOTOS.pop(m.from_user.id, None)
        await state.set_state(Add.photos)
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="✅ Готово")], [KeyboardButton(text="❌ Отмена")]],
            resize_keyboard=True,
        )
        await m.answer(
            f"📷 Отправьте фото (до {MAX_PHOTOS} штук, можно альбомом). Когда закончите — нажмите «✅ Готово».",
            reply_markup=kb,
        )


@router.message(Add.photos, F.photo, IS_ADMIN)
async def add_photo(m: Message):
    lst = PHOTOS[m.from_user.id]
    if len(lst) < MAX_PHOTOS:
        lst.append(m.photo[-1].file_id)


@router.message(Add.photos, F.text == "✅ Готово", IS_ADMIN)
async def add_photos_done(m: Message, state: FSMContext):
    photos = PHOTOS.pop(m.from_user.id, [])
    await state.update_data(photos=photos)
    d = await state.get_data()
    await m.answer(f"Фото: {len(photos)}. Вот как будет выглядеть объявление 👇", reply_markup=ReplyKeyboardRemove())
    if len(photos) == 1:
        await m.answer_photo(photos[0])
    elif photos:
        await m.answer_media_group([InputMediaPhoto(media=p) for p in photos])
    await m.answer("🇷🇺 <b>Русский канал:</b>\n\n" + build_post(None, d, "ru"))
    await m.answer("🇮🇱 <b>Ивритский канал:</b>\n\n" + build_post(None, d, "he"))
    await state.set_state(Add.confirm)
    await m.answer(
        "Публикуем?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="✅ Опубликовать", callback_data="pub:yes"),
                InlineKeyboardButton(text="❌ Отменить", callback_data="pub:no"),
            ]]
        ),
    )


@router.message(Add.photos, IS_ADMIN)
async def add_photos_other(m: Message):
    await m.answer("Отправьте фото или нажмите «✅ Готово».")


async def publish(bot: Bot, oid: int, d: dict) -> list[str]:
    report = []
    photos = d.get("photos") or []
    for lang, chat in CHANNELS.items():
        if not chat:
            continue
        text = build_post(oid, d, lang)
        try:
            ids: list[int] = []
            if photos and len(text) <= 1024:
                if len(photos) == 1:
                    msg = await bot.send_photo(chat, photos[0], caption=text)
                    ids = [msg.message_id]
                else:
                    media = [InputMediaPhoto(media=p) for p in photos]
                    media[0] = InputMediaPhoto(media=photos[0], caption=text)
                    ids = [x.message_id for x in await bot.send_media_group(chat, media)]
                main, kind = ids[0], "caption"
            else:
                if len(photos) == 1:
                    ids = [(await bot.send_photo(chat, photos[0])).message_id]
                elif photos:
                    ids = [x.message_id for x in await bot.send_media_group(chat, [InputMediaPhoto(media=p) for p in photos])]
                msg = await bot.send_message(chat, text)
                ids.append(msg.message_id)
                main, kind = msg.message_id, "text"
            db.execute(
                "INSERT INTO posts VALUES(?,?,?,?,?,?)", (oid, chat, lang, json.dumps(ids), main, kind)
            )
            report.append(f"✅ {chat}")
        except Exception as e:  # noqa: BLE001
            report.append(f"⚠️ {chat}: {escape(str(e))}")
    db.commit()
    return report


@router.callback_query(Add.confirm, F.data.startswith("pub:"))
async def add_confirm(c: CallbackQuery, state: FSMContext):
    lang = get_lang(c.from_user.id) or "ru"
    if c.data == "pub:no":
        await state.clear()
        await c.message.edit_text("Отменено.")
        await c.message.answer("Меню 👇", reply_markup=main_kb(lang))
        return await c.answer()
    d = await state.get_data()
    await state.clear()
    cur = db.execute("INSERT INTO objects(data) VALUES(?)", (json.dumps(d, ensure_ascii=False),))
    db.commit()
    oid = cur.lastrowid
    await c.message.edit_text(f"Публикую объект №{oid}…")
    report = await publish(c.bot, oid, d)
    await c.message.answer(f"Объект №{oid}:\n" + "\n".join(report), reply_markup=main_kb(lang))
    await c.answer()


# ======================= Клиент: меню =======================
async def user_lang(m: Message) -> str:
    return get_lang(m.from_user.id) or "ru"


@router.message(F.text.in_(btn_texts("btn_lang")))
async def menu_lang(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(CHOOSE_LANG, reply_markup=lang_kb())


@router.message(F.text.in_(btn_texts("btn_browse")))
async def menu_browse(m: Message, state: FSMContext):
    await state.clear()
    lang = await user_lang(m)
    url = channel_url(CHANNELS.get(lang, ""))
    if not url:
        return await m.answer(T[lang]["no_channel"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=T[lang]["open_channel"], url=url)]])
    await m.answer(T[lang]["browse_text"], reply_markup=kb)


@router.message(F.text.in_(btn_texts("btn_contact")))
async def menu_contact(m: Message, state: FSMContext):
    await state.clear()
    lang = await user_lang(m)
    await state.set_state(Ask.question)
    await m.answer(T[lang]["ask_question"])


@router.message(F.text.in_(btn_texts("btn_pick")))
async def menu_pick(m: Message, state: FSMContext):
    await state.clear()
    lang = await user_lang(m)
    await state.set_state(Pick.city)
    await m.answer(T[lang]["ask_city"], reply_markup=options_kb("pc", CITIES, lang, T[lang]["any"]))


@router.callback_query(Pick.city, F.data.startswith("pc:"))
async def pick_city(c: CallbackQuery, state: FSMContext):
    lang = get_lang(c.from_user.id) or "ru"
    key = c.data.split(":", 1)[1]
    await state.update_data(city=key)
    chosen = T[lang]["any"] if key == "any" else name_of(CITIES, key, lang)
    await c.message.edit_text(f"{T[lang]['ask_city']}\n✅ {chosen}")
    await state.set_state(Pick.ptype)
    await c.message.answer(T[lang]["ask_type"], reply_markup=options_kb("pt", TYPES, lang, T[lang]["any"]))
    await c.answer()


@router.callback_query(Pick.ptype, F.data.startswith("pt:"))
async def pick_type(c: CallbackQuery, state: FSMContext):
    lang = get_lang(c.from_user.id) or "ru"
    key = c.data.split(":", 1)[1]
    await state.update_data(ptype=key)
    chosen = T[lang]["any"] if key == "any" else name_of(TYPES, key, lang)
    await c.message.edit_text(f"{T[lang]['ask_type']}\n✅ {chosen}")
    await state.set_state(Pick.budget)
    await c.message.answer(T[lang]["ask_budget"])
    await c.answer()


@router.message(Pick.budget, F.text)
async def pick_budget(m: Message, state: FSMContext):
    lang = await user_lang(m)
    await state.update_data(budget=m.text.strip()[:100], kind="pick")
    await state.set_state(Lead.contact)
    await m.answer(T[lang]["ask_contact"], reply_markup=phone_kb(lang))


@router.message(Lead.contact, F.contact | F.text)
async def lead_contact(m: Message, state: FSMContext):
    lang = await user_lang(m)
    phone = m.contact.phone_number if m.contact else m.text.strip()
    if not m.contact and len(re.sub(r"\D", "", phone)) < 7:
        return await m.answer(T[lang]["invalid_phone"])
    d = await state.get_data()
    await state.clear()
    d["phone"] = phone
    db.execute(
        "INSERT INTO leads(user_id, kind, data) VALUES(?,?,?)",
        (m.from_user.id, d.get("kind"), json.dumps(d, ensure_ascii=False)),
    )
    db.commit()

    if d.get("kind") == "object":
        oid = d.get("obj_id")
        obj, status = get_object(oid)
        if obj:
            info = (
                f"{name_of(TYPES, obj['ptype'], 'ru')}, {name_of(CITIES, obj['city'], 'ru')}, "
                f"{fmt_num(obj['price'])} € ({status})"
            )
        else:
            info = "объект не найден"
        head = f"🔥 <b>Заявка по объекту №{oid}</b>\n{info}"
    else:
        city = "любой" if d.get("city") == "any" else name_of(CITIES, d.get("city"), "ru")
        ptype = "любой" if d.get("ptype") == "any" else name_of(TYPES, d.get("ptype"), "ru")
        head = (
            "📝 <b>Заявка на подбор</b>\n"
            f"Город: {city}\nТип: {ptype}\nБюджет: {escape(d.get('budget', '—'))}"
        )
    text = (
        f"{head}\n\nКлиент: {user_link(m.from_user)}\n📱 {escape(phone)}\nЯзык: {lang.upper()}\n\n"
        "↩️ Ответьте на это сообщение, чтобы написать клиенту через бота."
    )
    await notify_admins(m.bot, text, m.from_user.id)
    await m.answer(T[lang]["thanks"], reply_markup=main_kb(lang))


@router.message(Ask.question)
async def ask_question(m: Message, state: FSMContext):
    lang = await user_lang(m)
    if not m.text:
        return await m.answer(T[lang]["only_text"])
    await state.clear()
    db.execute(
        "INSERT INTO leads(user_id, kind, data) VALUES(?,?,?)",
        (m.from_user.id, "question", json.dumps({"text": m.text}, ensure_ascii=False)),
    )
    db.commit()
    await notify_admins(
        m.bot,
        f"❓ <b>Вопрос</b> от {user_link(m.from_user)} (язык: {lang.upper()}):\n\n{escape(m.text)}\n\n"
        "↩️ Ответьте на это сообщение, чтобы ответить клиенту.",
        m.from_user.id,
    )
    await m.answer(T[lang]["question_sent"], reply_markup=main_kb(lang))


@router.message()
async def fallback(m: Message):
    lang = get_lang(m.from_user.id)
    if not lang:
        return await m.answer(CHOOSE_LANG, reply_markup=lang_kb())
    await m.answer(T[lang]["fallback"], reply_markup=main_kb(lang))


# ======================= Запуск =======================
async def main():
    global BOT_USERNAME
    if not BOT_TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в файле .env")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    me = await bot.get_me()
    BOT_USERNAME = me.username
    log.info("Бот @%s запущен. Админы: %s. Каналы: %s", BOT_USERNAME, ADMIN_IDS or "не заданы", CHANNELS)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
