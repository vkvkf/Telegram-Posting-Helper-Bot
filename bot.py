"""
Телеграм-бот для постинга в КАНАЛ:
- Роли: Владелец (OWNER) и админы.
- У каждого админа свой канал (показывается "Название (ID)").
- Посты: текст (HTML) + фото + многорядные веб-кнопки.
- Шаблоны: добавление/удаление/список/предпросмотр/отправка.
- Экспорт/Импорт шаблонов: JSON (в меню "🧩 Управление шаблонами").
- Подключение канала: переслать пост или указать @username.
- Безопасный edit_text.
- storage.json создаётся автоматически (рядом с bot.py или в DATA_DIR).

Требования: Python 3.12+, aiogram 3.13+
"""

import asyncio
import base64
import html
import json
import os
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv


# ----------------------------- ХРАНИЛИЩЕ ----------------------------- #

BASE_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).resolve().parent)
BASE_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_FILE = BASE_DIR / "storage.json"

DEFAULT_STORAGE = {
    "admins": [],           # [user_id]
    "channels": {},         # {str(user_id): channel_id}
    "channel_titles": {},   # {str(user_id): "Title (id)"}
    "templates": {}         # {game: {cheat: {name: {text, photo, buttons}}}}
}

def load_storage() -> dict:
    if STORAGE_FILE.exists():
        try:
            return json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    STORAGE_FILE.write_text(json.dumps(DEFAULT_STORAGE, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(STORAGE_FILE.read_text(encoding="utf-8"))

def save_storage(data: dict) -> None:
    """Атомная запись, чтобы не бить файл при сбоях."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(BASE_DIR), prefix="storage_", suffix=".json")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STORAGE_FILE)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


# ----------------------------- МОДЕЛИ ----------------------------- #

@dataclass
class Button:
    t: str
    u: str

@dataclass
class Draft:
    text: str = ""
    buttons: List[List[Button]] = field(default_factory=list)
    photo: Optional[str] = None

    def as_markup(self) -> InlineKeyboardMarkup:
        rows = []
        for row in self.buttons:
            rows.append([InlineKeyboardButton(text=b.t, url=b.u) for b in row])
        return InlineKeyboardMarkup(inline_keyboard=rows)


# ----------------------------- FSM ----------------------------- #

class ComposeStates(StatesGroup):
    WAIT_TEXT = State()
    ADD_BUTTON_TEXT = State()
    ADD_BUTTON_URL = State()
    WAIT_PHOTO = State()

class ManageTemplateStates(StatesGroup):
    ADD_GAME = State()
    ADD_CHEAT = State()
    ADD_NAME = State()
    ADD_TEXT = State()
    ADD_PHOTO = State()
    ADD_BTN_TEXT = State()
    ADD_BTN_URL = State()
    BTN_MENU = State()

class SettingsStates(StatesGroup):
    CHOOSE_CONNECT_METHOD = State()
    WAIT_FORWARD_FROM_CHANNEL = State()
    WAIT_CHANNEL_USERNAME = State()
    WAIT_ADMIN_ADD = State()
    WAIT_ADMIN_REMOVE = State()

class ImportTemplatesStates(StatesGroup):
    WAIT_FILE = State()


# ----------------------------- ИНИЦИАЛИЗАЦИЯ ----------------------------- #

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

storage = load_storage()
seed_admins = set(storage.get("admins", [])) | set(ADMIN_IDS)
if OWNER_ID:
    seed_admins.add(OWNER_ID)
storage["admins"] = sorted(seed_admins)
save_storage(storage)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
user_drafts: Dict[int, Draft] = {}


# ----------------------------- УТИЛИТЫ ----------------------------- #

def is_owner(uid: int) -> bool:
    return OWNER_ID and uid == OWNER_ID

def is_admin(uid: int) -> bool:
    return uid in set(storage.get("admins", []))

def admin_only(uid: int) -> bool:
    return is_owner(uid) or is_admin(uid)

async def safe_edit_text(msg: Message, text: str, **kwargs):
    """Лечим 'message is not modified'."""
    try:
        await msg.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await msg.edit_text(text + "\u200B", **kwargs)
        else:
            raise

def back_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="menu:back")
    return kb.as_markup()

def channel_label_for_user(uid: int) -> str:
    label = storage.get("channel_titles", {}).get(str(uid))
    return label if label else "не подключён"

def main_menu_kb(uid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Создать пост", callback_data="menu:create")
    kb.button(text="📚 Готовые посты", callback_data="menu:ready")
    kb.button(text="🧩 Управление шаблонами", callback_data="menu:manage")
    kb.button(text=f"⚙️ Канал: {channel_label_for_user(uid)}", callback_data="menu:settings")
    if is_owner(uid):
        kb.button(text="👥 Админы и каналы", callback_data="owner:panel")
    kb.adjust(2, 2)
    return kb.as_markup()

def settings_menu_kb(uid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Канал: {channel_label_for_user(uid)}", callback_data="noop")
    kb.button(text="📩 Подключить канал", callback_data="set:connect")
    kb.button(text="🧪 Проверить отправку", callback_data="set:test")
    kb.button(text="❌ Очистить канал", callback_data="set:clear")
    if is_owner(uid):
        kb.button(text="👤 Добавить админа", callback_data="set:add_admin")
        kb.button(text="🗑 Удалить админа", callback_data="set:del_admin")
        kb.button(text="📜 Список админов", callback_data="set:list_admins")
    kb.button(text="⬅️ В меню", callback_data="menu:back")
    kb.adjust(1, 2, 1, 2, 1)
    return kb.as_markup()

def compose_kb(draft: Optional[Draft] = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить кнопку", callback_data="compose:add_btn")
    kb.button(text="⤵️ Добавить ряд", callback_data="compose:add_row")
    kb.button(text="🖼 Добавить фото", callback_data="compose:add_photo")
    if draft and draft.photo:
        kb.button(text="🧹 Удалить фото", callback_data="compose:del_photo")
    kb.button(text="🔍 Предпросмотр", callback_data="compose:preview")
    kb.button(text="📤 Отправить в канал", callback_data="compose:send")
    kb.button(text="⬅️ В меню", callback_data="menu:back")
    kb.adjust(2, 2, 2)
    return kb.as_markup()

def build_matrix_preview(buttons: List[List[Button]]) -> str:
    if not buttons:
        return "(кнопок нет)"
    lines = []
    for i, row in enumerate(buttons, start=1):
        cols = [f"{b.t} ({b.u})" for b in row]
        lines.append(f"Ряд {i}: " + " | ".join(cols))
    return "\n".join(lines)

def templates_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for game in storage.get("templates", {}):
        kb.button(text=game, callback_data=f"tpl:g:{game}")
    kb.button(text="⬅️ В меню", callback_data="menu:back")
    kb.adjust(2)
    return kb.as_markup()

def cheats_menu(game: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cheat in storage.get("templates", {}).get(game, {}):
        kb.button(text=cheat, callback_data=f"tpl:c:{game}:{cheat}")
    kb.button(text="⬅️ Назад", callback_data="tpl:back:games")
    kb.adjust(2)
    return kb.as_markup()

def templates_list_menu(game: str, cheat: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for name in storage.get("templates", {}).get(game, {}).get(cheat, {}):
        kb.button(text=name, callback_data=f"tpl:n:{game}:{cheat}:{name}")
    kb.button(text="⬅️ Назад", callback_data=f"tpl:back:cheats:{game}")
    kb.adjust(2)
    return kb.as_markup()

def template_view_kb(game: str, cheat: str, name: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Предпросмотр", callback_data=f"tpl:prev:{game}:{cheat}:{name}")
    kb.button(text="📤 Отправить в канал", callback_data=f"tpl:send:{game}:{cheat}:{name}")
    kb.button(text="⬅️ Назад", callback_data=f"tpl:back:templates:{game}:{cheat}")
    kb.adjust(2, 1)
    return kb.as_markup()

def manage_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить шаблон", callback_data="m:add")
    kb.button(text="🗑 Удалить шаблон", callback_data="m:del")
    kb.button(text="📜 Список шаблонов", callback_data="m:list")
    kb.button(text="📦 Экспорт шаблонов", callback_data="m:export")
    kb.button(text="📥 Импорт шаблонов", callback_data="m:import")
    kb.button(text="⬅️ В меню", callback_data="menu:back")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def matrix_to_markup(matrix: List[List[Dict[str, str]]]) -> InlineKeyboardMarkup:
    rows = []
    for row in matrix:
        rows.append([InlineKeyboardButton(text=btn["t"], url=btn["u"]) for btn in row])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ----------------------------- ССЫЛКИ (для панели владельца) ----------------------------- #

async def get_user_display(user_id: int) -> Tuple[str, str]:
    try:
        chat: Chat = await bot.get_chat(user_id)
        name = html.escape(chat.full_name or "user")
        if chat.username:
            return f'<a href="https://t.me/{chat.username}">{name}</a>', name
        return f'<a href="tg://user?id={user_id}">{name}</a>', name
    except Exception:
        return f'<a href="tg://user?id={user_id}">{user_id}</a>', str(user_id)

async def get_channel_display(channel_id: int) -> str:
    try:
        chat: Chat = await bot.get_chat(channel_id)
        title = html.escape(chat.title or "Канал")
        if chat.username:
            return f'<a href="https://t.me/{chat.username}">{title}</a> (<code>{channel_id}</code>)'
        return f'{title} (<code>{channel_id}</code>)'
    except Exception:
        return f'канал (<code>{channel_id}</code>)'


# ----------------------------- ГЛОБАЛЬНАЯ ЗАЩИТА ----------------------------- #

class AdminGuard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        from aiogram.types import Message, CallbackQuery
        uid = None
        if isinstance(event, Message) and event.from_user:
            uid = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            uid = event.from_user.id

        if not uid:
            return await handler(event, data)

        if not admin_only(uid):
            if isinstance(event, Message):
                await event.answer("⛔️ Доступ только для админов.")
            else:
                await event.answer("⛔️ Доступ только для админов.", show_alert=True)
            return
        return await handler(event, data)

dp.message.middleware(AdminGuard())
dp.callback_query.middleware(AdminGuard())


# ----------------------------- КОМАНДЫ ----------------------------- #

@dp.message(CommandStart())
async def start_cmd(m: Message):
    await m.answer("👋 Привет! Это бот для постов в канал.", reply_markup=main_menu_kb(m.from_user.id))

@dp.message(Command("echo_id"))
async def echo_id(m: Message):
    await m.answer(f"chat_id: <code>{m.chat.id}</code>\nuser_id: <code>{m.from_user.id}</code>")

@dp.message(Command("storage"))
async def show_storage(m: Message):
    try:
        import time
        file_exists = STORAGE_FILE.exists()
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STORAGE_FILE.stat().st_mtime)) if file_exists else "—"
        mem_preview = json.dumps(storage, ensure_ascii=False)[:800]
        await m.answer(
            "🧾 <b>storage.json</b>\n"
            f"Путь: <code>{STORAGE_FILE}</code>\n"
            f"Есть файл: <b>{'да' if file_exists else 'нет'}</b>\n"
            f"Изменён: <b>{mtime}</b>\n\n"
            f"<b>В памяти:</b>\n<code>{mem_preview}</code>"
        )
    except Exception as e:
        await m.answer(f"❌ Ошибка чтения: {e}")


# ----------------------------- НАВИГАЦИЯ ----------------------------- #

@dp.callback_query(F.data == "menu:back")
async def menu_back(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_text(c.message, "📋 Главное меню:", reply_markup=main_menu_kb(c.from_user.id))
    await c.answer()


# ----------------------------- СОЗДАТЬ ПОСТ ----------------------------- #

@dp.callback_query(F.data == "menu:create")
async def create_post(c: CallbackQuery, state: FSMContext):
    user_drafts[c.from_user.id] = Draft()
    await state.set_state(ComposeStates.WAIT_TEXT)
    await safe_edit_text(c.message, "✏️ Отправь <b>текст поста</b> (HTML допустим).", reply_markup=back_menu_kb())
    await c.answer()

@dp.message(ComposeStates.WAIT_TEXT)
async def get_post_text(m: Message, state: FSMContext):
    d = user_drafts.get(m.from_user.id, Draft())
    d.text = m.html_text or m.text or ""
    user_drafts[m.from_user.id] = d
    await state.clear()
    await m.answer(f"✅ Текст сохранён.\n\n{build_matrix_preview(d.buttons)}", reply_markup=compose_kb(d))

@dp.callback_query(F.data.startswith("compose:"))
async def compose_actions(c: CallbackQuery, state: FSMContext):
    d = user_drafts.get(c.from_user.id, Draft())
    user_drafts[c.from_user.id] = d
    action = c.data.split(":")[1]

    if action == "add_btn":
        await state.set_state(ComposeStates.ADD_BUTTON_TEXT)
        await safe_edit_text(c.message, "🆕 Введи <b>текст кнопки</b>:", reply_markup=back_menu_kb())

    elif action == "add_row":
        d.buttons.append([])
        await safe_edit_text(c.message, "Добавлен новый ряд кнопок.", reply_markup=compose_kb(d))

    elif action == "add_photo":
        await state.set_state(ComposeStates.WAIT_PHOTO)
        await safe_edit_text(c.message, "📷 Пришли <b>фото</b> (не как файл).", reply_markup=back_menu_kb())

    elif action == "del_photo":
        d.photo = None
        await safe_edit_text(c.message, "🧹 Фото удалено.", reply_markup=compose_kb(d))

    elif action == "preview":
        await preview_post(c, d)

    elif action == "send":
        await send_post_to_channel(c, d)

    await c.answer()

@dp.message(ComposeStates.WAIT_PHOTO, F.photo)
async def add_photo(m: Message, state: FSMContext):
    d = user_drafts.get(m.from_user.id, Draft())
    d.photo = m.photo[-1].file_id
    user_drafts[m.from_user.id] = d
    await state.clear()
    await m.answer("✅ Фото сохранено.", reply_markup=compose_kb(d))

@dp.message(ComposeStates.ADD_BUTTON_TEXT)
async def add_btn_text(m: Message, state: FSMContext):
    await state.update_data(btn_text=m.text or "")
    await state.set_state(ComposeStates.ADD_BUTTON_URL)
    await m.answer("🔗 Теперь пришли <b>URL</b> (http/https):", reply_markup=back_menu_kb())

@dp.message(ComposeStates.ADD_BUTTON_URL)
async def add_btn_url(m: Message, state: FSMContext):
    data = await state.get_data()
    text = data.get("btn_text", "")
    url = (m.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return await m.answer("❌ URL должен начинаться с http:// или https://")

    d = user_drafts.get(m.from_user.id, Draft())
    if not d.buttons:
        d.buttons.append([])
    d.buttons[-1].append(Button(t=text, u=url))
    user_drafts[m.from_user.id] = d
    await state.clear()
    await m.answer(f"✅ Кнопка добавлена.\n\n{build_matrix_preview(d.buttons)}", reply_markup=compose_kb(d))

async def preview_post(c: CallbackQuery, d: Draft):
    if d.photo:
        await c.message.answer_photo(d.photo, caption=d.text, reply_markup=d.as_markup())
    else:
        await c.message.answer(d.text, reply_markup=d.as_markup())
    await c.answer("📤 Предпросмотр отправлен выше")

async def send_post_to_channel(c: CallbackQuery, d: Draft):
    ch = storage.get("channels", {}).get(str(c.from_user.id))
    if not ch:
        return await c.answer("⚠️ Сначала подключи свой канал в ⚙️ Настройках", show_alert=True)
    try:
        if d.photo:
            await bot.send_photo(chat_id=ch, photo=d.photo, caption=d.text, reply_markup=d.as_markup())
        else:
            await bot.send_message(chat_id=ch, text=d.text, reply_markup=d.as_markup())
        await c.answer("✅ Отправлено в твой канал!", show_alert=True)
    except Exception as e:
        await c.answer(f"❌ Ошибка отправки: {e}", show_alert=True)


# ----------------------------- ГОТОВЫЕ ПОСТЫ ----------------------------- #

@dp.callback_query(F.data == "menu:ready")
async def ready_root(c: CallbackQuery):
    if not storage.get("templates"):
        return await c.answer("📂 Нет сохранённых шаблонов", show_alert=True)
    await safe_edit_text(c.message, "📚 Выбери игру:", reply_markup=templates_menu())
    await c.answer()

@dp.callback_query(F.data.startswith("tpl:g:"))
async def ready_choose_game(c: CallbackQuery):
    game = c.data.split(":")[2]
    await safe_edit_text(c.message, f"🎮 <b>{game}</b>\nВыбери чит:", reply_markup=cheats_menu(game))
    await c.answer()

@dp.callback_query(F.data.startswith("tpl:c:"))
async def ready_choose_cheat(c: CallbackQuery):
    _, _, game, cheat = c.data.split(":")
    await safe_edit_text(
        c.message,
        f"🎮 <b>{game}</b> → 💾 <b>{cheat}</b>\nВыбери шаблон:",
        reply_markup=templates_list_menu(game, cheat)
    )
    await c.answer()

@dp.callback_query(F.data.startswith("tpl:n:"))
async def ready_choose_name(c: CallbackQuery):
    _, _, game, cheat, name = c.data.split(":")
    t = storage["templates"][game][cheat][name]
    text = t.get("text", "")
    await safe_edit_text(c.message, f"Шаблон: {game} / {cheat} / {name}\n\n{text}",
                         reply_markup=template_view_kb(game, cheat, name))
    await c.answer()

@dp.callback_query(F.data == "tpl:back:games")
async def back_to_games(c: CallbackQuery):
    await safe_edit_text(c.message, "📚 Выбери игру:", reply_markup=templates_menu())
    await c.answer()

@dp.callback_query(F.data.startswith("tpl:back:cheats:"))
async def back_to_cheats(c: CallbackQuery):
    game = c.data.split(":")[3]
    await safe_edit_text(c.message, f"🎮 <b>{game}</b>\nВыбери чит:", reply_markup=cheats_menu(game))
    await c.answer()

@dp.callback_query(F.data.startswith("tpl:back:templates:"))
async def back_to_templates(c: CallbackQuery):
    _, _, _, game, cheat = c.data.split(":")
    await safe_edit_text(
        c.message,
        f"🎮 <b>{game}</b> → 💾 <b>{cheat}</b>\nВыбери шаблон:",
        reply_markup=templates_list_menu(game, cheat)
    )
    await c.answer()

@dp.callback_query(F.data.startswith("tpl:prev:"))
async def tpl_preview(c: CallbackQuery):
    _, _, game, cheat, name = c.data.split(":")
    t = storage["templates"][game][cheat][name]
    text = t.get("text", "")
    photo = t.get("photo")
    buttons = t.get("buttons", [])
    kb = matrix_to_markup(buttons)
    if photo:
        await c.message.answer_photo(photo=photo, caption=text, reply_markup=kb)
    else:
        await c.message.answer(text, reply_markup=kb)
    await c.answer("Предпросмотр отправлен выше")

@dp.callback_query(F.data.startswith("tpl:send:"))
async def tpl_send(c: CallbackQuery):
    ch = storage.get("channels", {}).get(str(c.from_user.id))
    if not ch:
        return await c.answer("⚠️ Сначала подключи свой канал в ⚙️ Настройках", show_alert=True)
    _, _, game, cheat, name = c.data.split(":")
    t = storage["templates"][game][cheat][name]
    text = t.get("text", "")
    photo = t.get("photo")
    buttons = t.get("buttons", [])
    kb = matrix_to_markup(buttons)
    try:
        if photo:
            await bot.send_photo(chat_id=ch, photo=photo, caption=text, reply_markup=kb)
        else:
            await bot.send_message(chat_id=ch, text=text, reply_markup=kb)
        await c.answer("✅ Отправлено в твой канал!", show_alert=True)
    except Exception as e:
        await c.answer(f"❌ Ошибка: {e}", show_alert=True)


# ----------------------------- УПРАВЛЕНИЕ ШАБЛОНАМИ ----------------------------- #

@dp.callback_query(F.data == "menu:manage")
async def manage_root(c: CallbackQuery):
    await safe_edit_text(c.message, "🧩 Управление шаблонами:", reply_markup=manage_menu())
    await c.answer()

# Добавление
@dp.callback_query(F.data == "m:add")
async def m_add_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(ManageTemplateStates.ADD_GAME)
    await safe_edit_text(c.message, "🎮 Введи название игры:", reply_markup=back_menu_kb())
    await c.answer()

@dp.message(ManageTemplateStates.ADD_GAME)
async def m_add_game(m: Message, state: FSMContext):
    await state.update_data(game=(m.text or "").strip())
    await state.set_state(ManageTemplateStates.ADD_CHEAT)
    await m.answer("💾 Введи название чита:", reply_markup=back_menu_kb())

@dp.message(ManageTemplateStates.ADD_CHEAT)
async def m_add_cheat(m: Message, state: FSMContext):
    await state.update_data(cheat=(m.text or "").strip())
    await state.set_state(ManageTemplateStates.ADD_NAME)
    await m.answer("🏷 Введи название шаблона:", reply_markup=back_menu_kb())

@dp.message(ManageTemplateStates.ADD_NAME)
async def m_add_name(m: Message, state: FSMContext):
    await state.update_data(name=(m.text or "").strip())
    await state.set_state(ManageTemplateStates.ADD_TEXT)
    await m.answer("✏️ Введи текст шаблона (HTML допустим):", reply_markup=back_menu_kb())

@dp.message(ManageTemplateStates.ADD_TEXT)
async def m_add_text(m: Message, state: FSMContext):
    await state.update_data(text=m.html_text or m.text or "")
    await state.set_state(ManageTemplateStates.ADD_PHOTO)
    await m.answer("📷 Пришли фото (или отправь 0, чтобы без фото):", reply_markup=back_menu_kb())

@dp.message(ManageTemplateStates.ADD_PHOTO)
async def m_add_photo(m: Message, state: FSMContext):
    if m.photo:
        await state.update_data(photo=m.photo[-1].file_id)
    elif (m.text or "").strip() == "0":
        await state.update_data(photo=None)
    else:
        return await m.answer("Пришли фото или отправь 0 для пропуска.", reply_markup=back_menu_kb())
    await state.set_state(ManageTemplateStates.ADD_BTN_TEXT)
    await m.answer("➕ Введи текст кнопки (или 0 — перейти к сохранению):", reply_markup=back_menu_kb())

@dp.message(ManageTemplateStates.ADD_BTN_TEXT)
async def m_btn_text(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if txt == "0":
        await finalize_template(state, [])
        return
    await state.update_data(btn_text=txt)
    await state.set_state(ManageTemplateStates.ADD_BTN_URL)
    await m.answer("🔗 Введи URL для этой кнопки:", reply_markup=back_menu_kb())

@dp.message(ManageTemplateStates.ADD_BTN_URL)
async def m_btn_url(m: Message, state: FSMContext):
    url = (m.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return await m.answer("❌ URL должен начинаться с http:// или https://")
    data = await state.get_data()
    matrix = data.get("matrix", [])
    row = data.get("row", [])
    row.append({"t": data.get("btn_text"), "u": url})
    await state.update_data(row=row, matrix=matrix)

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Ещё кнопка в ряд", callback_data="mt:add_in_row")
    kb.button(text="⤵️ Новый ряд", callback_data="mt:new_row")
    kb.button(text="✅ Сохранить шаблон", callback_data="mt:save")
    kb.button(text="⬅️ В меню", callback_data="menu:manage")
    kb.adjust(1, 1, 1, 1)
    await state.set_state(ManageTemplateStates.BTN_MENU)
    await m.answer("Кнопка добавлена. Что дальше?", reply_markup=kb.as_markup())

@dp.callback_query(ManageTemplateStates.BTN_MENU, F.data.startswith("mt:"))
async def m_btn_menu(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    row = data.get("row", [])
    matrix = data.get("matrix", [])

    if c.data == "mt:add_in_row":
        await state.set_state(ManageTemplateStates.ADD_BTN_TEXT)
        await safe_edit_text(c.message, "Текст следующей кнопки в ЭТОТ ряд:", reply_markup=back_menu_kb())

    elif c.data == "mt:new_row":
        if row:
            matrix.append(row)
            await state.update_data(matrix=matrix, row=[])
        await state.set_state(ManageTemplateStates.ADD_BTN_TEXT)
        await safe_edit_text(c.message, "Текст первой кнопки в НОВЫЙ ряд:", reply_markup=back_menu_kb())

    elif c.data == "mt:save":
        if row:
            matrix.append(row)
        await finalize_template(state, matrix)
        await safe_edit_text(c.message, "✅ Шаблон сохранён.", reply_markup=manage_menu())

    await c.answer()

async def finalize_template(state: FSMContext, matrix: List[List[Dict[str, str]]]):
    data = await state.get_data()
    game, cheat, name = data["game"], data["cheat"], data["name"]
    text, photo = data["text"], data.get("photo")
    storage.setdefault("templates", {}).setdefault(game, {}).setdefault(cheat, {})[name] = {
        "text": text,
        "photo": photo,
        "buttons": matrix
    }
    save_storage(storage)
    await state.clear()

# Удаление (payload = base64(json([game, cheat, name])))
@dp.callback_query(F.data == "m:del")
async def delete_template_start(c: CallbackQuery):
    tpls = storage.get("templates", {})
    if not tpls:
        return await c.answer("📂 Нет сохранённых шаблонов", show_alert=True)

    kb = InlineKeyboardBuilder()
    for g, cheats in tpls.items():
        for ch, names in cheats.items():
            for n in names.keys():
                payload = base64.urlsafe_b64encode(json.dumps([g, ch, n]).encode("utf-8")).decode("ascii")
                kb.button(text=f"{g} / {ch} / {n}", callback_data=f"m:delete:{payload}")
    kb.button(text="⬅️ Назад", callback_data="menu:manage")
    kb.adjust(1)
    await safe_edit_text(c.message, "🗑 Выбери шаблон для удаления:", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data.startswith("m:delete:"))
async def delete_template_confirm(c: CallbackQuery):
    b64 = c.data.split(":", 2)[-1]
    try:
        g, ch, n = json.loads(base64.urlsafe_b64decode(b64.encode("ascii")))
    except Exception:
        return await c.answer("❌ Ошибка формата", show_alert=True)

    try:
        del storage["templates"][g][ch][n]
        if not storage["templates"][g][ch]:
            del storage["templates"][g][ch]
        if not storage["templates"][g]:
            del storage["templates"][g]
        save_storage(storage)
        await c.answer("✅ Шаблон удалён", show_alert=True)
        await safe_edit_text(c.message, "🧩 Управление шаблонами:", reply_markup=manage_menu())
    except KeyError:
        await c.answer("❌ Не найден (возможно уже удалён)", show_alert=True)

# Список (Game -> Cheat -> name1, name2)
@dp.callback_query(F.data == "m:list")
async def list_templates(c: CallbackQuery):
    tpls = storage.get("templates", {})
    if not tpls:
        return await c.answer("📂 Нет сохранённых шаблонов", show_alert=True)

    lines = []
    for game in sorted(tpls.keys(), key=str.lower):
        for cheat in sorted(tpls[game].keys(), key=str.lower):
            names = sorted(tpls[game][cheat].keys(), key=str.lower)
            lines.append(f"{game} -> {cheat} -> {', '.join(names)}")

    body = "\n".join(lines)
    text = "📜 <b>Список шаблонов</b>\n\n" + html.escape(body)

    if len(text) > 3500:
        doc = BufferedInputFile(body.encode("utf-8"), filename="templates_list.txt")
        await c.message.answer_document(document=doc, caption="📜 Список шаблонов (Игра -> Чит -> названия)")
        return await c.answer()

    await safe_edit_text(c.message, text, reply_markup=manage_menu())
    await c.answer()

# Экспорт / Импорт
@dp.callback_query(F.data == "m:export")
async def m_export(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        return await c.answer("⛔️ Доступ только для админов.", show_alert=True)
    payload = json.dumps(storage.get("templates", {}), ensure_ascii=False, indent=2).encode("utf-8")
    doc = BufferedInputFile(payload, filename="templates_export.json")
    await c.message.answer_document(document=doc, caption="📦 Экспорт шаблонов (JSON).")
    await c.answer()

@dp.callback_query(F.data == "m:import")
async def m_import_start(c: CallbackQuery, state: FSMContext):
    if not admin_only(c.from_user.id):
        return await c.answer("⛔️ Доступ только для админов.", show_alert=True)
    await state.set_state(ImportTemplatesStates.WAIT_FILE)
    await c.message.answer("📥 Пришли файл <b>templates_export.json</b> (как документ).")
    await c.answer()

@dp.message(ImportTemplatesStates.WAIT_FILE, F.document)
async def m_import_file(m: Message, state: FSMContext):
    if not admin_only(m.from_user.id):
        return await m.answer("⛔️ Доступ только для админов.")
    try:
        buf = BytesIO()
        await bot.download(m.document, destination=buf)
        buf.seek(0)
        incoming = json.load(buf)
        if not isinstance(incoming, dict):
            return await m.answer("❌ Неверный формат: нужен объект {game: {cheat: {name: {...}}}}")

        merged = 0
        templates = storage.setdefault("templates", {})
        for game, cheats in incoming.items():
            if not isinstance(cheats, dict):
                continue
            g = templates.setdefault(game, {})
            for cheat, names in cheats.items():
                if not isinstance(names, dict):
                    continue
                ch = g.setdefault(cheat, {})
                for name, payload in names.items():
                    if not isinstance(payload, dict):
                        continue
                    text = payload.get("text", "")
                    photo = payload.get("photo")
                    buttons = payload.get("buttons", [])
                    ch[name] = {"text": text, "photo": photo, "buttons": buttons}
                    merged += 1

        save_storage(storage)
        await state.clear()
        await m.answer(f"✅ Импорт завершён. Шаблонов добавлено/обновлено: <b>{merged}</b>.")
    except Exception as e:
        await m.answer(f"❌ Ошибка импорта: {e}")

@dp.message(ImportTemplatesStates.WAIT_FILE)
async def m_import_wrong(m: Message):
    await m.answer("Пришли файл-документ JSON (templates_export.json).")


# ----------------------------- НАСТРОЙКИ: канал и роли ----------------------------- #

@dp.callback_query(F.data == "menu:settings")
async def settings_root(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_text(c.message, "⚙️ Настройки:", reply_markup=settings_menu_kb(c.from_user.id))
    await c.answer()

@dp.callback_query(F.data == "set:clear")
async def set_clear(c: CallbackQuery):
    key = str(c.from_user.id)
    storage.setdefault("channels", {}).pop(key, None)
    storage.setdefault("channel_titles", {}).pop(key, None)
    save_storage(storage)
    await safe_edit_text(c.message, "Канал очищен.", reply_markup=settings_menu_kb(c.from_user.id))
    await c.answer()

@dp.callback_query(F.data == "set:test")
async def set_test(c: CallbackQuery):
    ch = storage.get("channels", {}).get(str(c.from_user.id))
    if not ch:
        return await c.answer("Канал не подключён", show_alert=True)
    try:
        await bot.send_message(ch, "🧪 Тест: бот может отправлять сообщения в канал.")
        await c.answer("✅ Тест отправлен. Если не видишь — назначь бота админом в канале.", show_alert=True)
    except Exception as e:
        await c.answer(f"❌ Ошибка: {e}", show_alert=True)

@dp.callback_query(F.data == "set:connect")
async def set_connect(c: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.CHOOSE_CONNECT_METHOD)
    kb = InlineKeyboardBuilder()
    kb.button(text="➡️ Перешлите пост из канала", callback_data="set:via_forward")
    kb.button(text="✏️ Указать @username", callback_data="set:via_username")
    kb.button(text="⬅️ В настройки", callback_data="menu:settings")
    kb.adjust(1, 1, 1)
    await safe_edit_text(c.message, "Как подключить канал?", reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(SettingsStates.CHOOSE_CONNECT_METHOD, F.data == "set:via_forward")
async def connect_via_forward(c: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.WAIT_FORWARD_FROM_CHANNEL)
    await safe_edit_text(
        c.message,
        "Перешли мне <b>любой пост</b> из нужного канала. Я возьму его ID и сохраню.",
        reply_markup=back_menu_kb()
    )
    await c.answer()

@dp.message(SettingsStates.WAIT_FORWARD_FROM_CHANNEL)
async def get_channel_from_forward(m: Message, state: FSMContext):
    if m.forward_from_chat and m.forward_from_chat.type == ChatType.CHANNEL:
        key = str(m.from_user.id)
        ch_id = m.forward_from_chat.id
        title = (m.forward_from_chat.title or "Канал").strip()
        label = f"{title} ({ch_id})"
        storage.setdefault("channels", {})[key] = ch_id
        storage.setdefault("channel_titles", {})[key] = label
        save_storage(storage)
        await state.clear()
        await m.answer(
            f"✅ Канал подключён: <b>{html.escape(title)}</b> (<code>{ch_id}</code>)",
            reply_markup=settings_menu_kb(m.from_user.id)
        )
    else:
        await m.answer("Это не пересланный пост из канала. Попробуй ещё раз.", reply_markup=back_menu_kb())

@dp.callback_query(SettingsStates.CHOOSE_CONNECT_METHOD, F.data == "set:via_username")
async def connect_via_username(c: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.WAIT_CHANNEL_USERNAME)
    await safe_edit_text(
        c.message,
        "Отправь <b>@username</b> канала (например, @glavnoe24). Бот узнает его ID.",
        reply_markup=back_menu_kb()
    )
    await c.answer()

@dp.message(SettingsStates.WAIT_CHANNEL_USERNAME)
async def get_channel_from_username(m: Message, state: FSMContext):
    username = (m.text or "").strip()
    if not username.startswith("@"):
        return await m.answer("Нужен формат @username", reply_markup=back_menu_kb())
    try:
        chat: Chat = await bot.get_chat(username)
        if chat.type != ChatType.CHANNEL:
            return await m.answer("Это не канал. Укажи @username именно канала.", reply_markup=back_menu_kb())
        key = str(m.from_user.id)
        ch_id = chat.id
        title = (chat.title or "Канал").strip()
        label = f"{title} ({ch_id})"
        storage.setdefault("channels", {})[key] = ch_id
        storage.setdefault("channel_titles", {})[key] = label
        save_storage(storage)
        await state.clear()
        await m.answer(
            f"✅ Канал подключён: <b>{html.escape(title)}</b> (<code>{ch_id}</code>)\n"
            f"Если отправка не идёт — назначь бота админом в канале.",
            reply_markup=settings_menu_kb(m.from_user.id)
        )
    except Exception as e:
        await m.answer(f"❌ Не удалось получить канал: {e}", reply_markup=back_menu_kb())


# ----------------------------- ВЛАДЕЛЕЦ: ПАНЕЛЬ ----------------------------- #

@dp.callback_query(F.data == "owner:panel")
async def owner_panel(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        return await c.answer("Только для владельца", show_alert=True)

    admins = storage.get("admins", [])
    channels = storage.get("channels", {})

    lines: List[str] = []
    for uid in admins:
        user_html, _ = await get_user_display(uid)
        tag = " (OWNER)" if uid == OWNER_ID else ""
        ch_id = channels.get(str(uid))
        if ch_id:
            ch_html = await get_channel_display(ch_id)
            lines.append(f"• {user_html}{tag} — {ch_html}")
        else:
            lines.append(f"• {user_html}{tag} — канал не подключён")

    text = "👥 <b>Админы и их каналы</b>\n" + ("\n".join(lines) if lines else "пусто")

    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Добавить админа", callback_data="set:add_admin")
    kb.button(text="🗑 Удалить админа", callback_data="set:del_admin")
    kb.button(text="📜 Список админов", callback_data="set:list_admins")
    kb.button(text="⬅️ В меню", callback_data="menu:back")
    kb.adjust(2, 2)
    await safe_edit_text(c.message, text, reply_markup=kb.as_markup())
    await c.answer()

@dp.callback_query(F.data == "set:add_admin")
async def set_add_admin(c: CallbackQuery, state: FSMContext):
    if not is_owner(c.from_user.id):
        return await c.answer("Только для владельца", show_alert=True)
    await state.set_state(SettingsStates.WAIT_ADMIN_ADD)
    await safe_edit_text(c.message, "Пришли <b>user_id</b> админа (число).", reply_markup=back_menu_kb())
    await c.answer()

@dp.message(SettingsStates.WAIT_ADMIN_ADD)
async def add_admin(m: Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        return await m.answer("Только для владельца.")
    try:
        uid = int((m.text or "").strip())
    except ValueError:
        return await m.answer("Нужно число. Пришли user_id снова.", reply_markup=back_menu_kb())
    admins = set(storage.get("admins", []))
    admins.add(uid)
    storage["admins"] = sorted(list(admins))
    save_storage(storage)
    await state.clear()
    await m.answer("✅ Админ добавлен.", reply_markup=main_menu_kb(m.from_user.id))

@dp.callback_query(F.data == "set:del_admin")
async def set_del_admin(c: CallbackQuery, state: FSMContext):
    if not is_owner(c.from_user.id):
        return await c.answer("Только для владельца", show_alert=True)
    if not storage.get("admins"):
        return await c.answer("Список админов пуст", show_alert=True)
    await state.set_state(SettingsStates.WAIT_ADMIN_REMOVE)
    await safe_edit_text(c.message, "Пришли <b>user_id</b> админа для удаления.", reply_markup=back_menu_kb())
    await c.answer()

@dp.message(SettingsStates.WAIT_ADMIN_REMOVE)
async def del_admin(m: Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        return await m.answer("Только для владельца.")
    try:
        uid = int((m.text or "").strip())
    except ValueError:
        return await m.answer("Нужно число. Пришли user_id снова.", reply_markup=back_menu_kb())
    admins = set(storage.get("admins", []))
    if uid in admins:
        admins.remove(uid)
        storage["admins"] = sorted(list(admins))
        save_storage(storage)
        msg = "🗑 Админ удалён."
    else:
        msg = "Такого админа нет."
    await state.clear()
    await m.answer(msg, reply_markup=main_menu_kb(m.from_user.id))

@dp.callback_query(F.data == "set:list_admins")
async def list_admins(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        return await c.answer("Только для владельца", show_alert=True)

    admins = storage.get("admins", [])
    channels = storage.get("channels", {})

    lines: List[str] = []
    for uid in admins:
        user_html, _ = await get_user_display(uid)
        tag = " (OWNER)" if uid == OWNER_ID else ""
        ch_id = channels.get(str(uid))
        if ch_id:
            ch_html = await get_channel_display(ch_id)
            lines.append(f"• {user_html}{tag} — {ch_html}")
        else:
            lines.append(f"• {user_html}{tag} — канал не подключён")

    txt = "📜 <b>Админы</b>\n" + ("\n".join(lines) if lines else "пусто")
    await c.message.answer(txt, disable_web_page_preview=True)
    await c.answer()


# ----------------------------- ЗАПУСК ----------------------------- #

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("❌ Укажи BOT_TOKEN в .env")
    print("✅ Bot started")
    print(f"🗂 storage.json path: {STORAGE_FILE}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Bot stopped")
