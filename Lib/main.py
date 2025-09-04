import os
import re
import asyncio
from typing import Optional
from datetime import datetime, time as dtime

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery
)
from dotenv import load_dotenv

# ---------- ENV ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ONEC_URL  = os.getenv("ONEC_URL")                 # например https://host/ib/hs/hr/check_passport
ONEC_USER = os.getenv("ONEC_USER")
ONEC_PASS = os.getenv("ONEC_PASS")
ONEC_DECISION_URL = os.getenv("ONEC_DECISION_URL")# например https://host/ib/hs/hr/tardy_decision
DB_PATH   = os.getenv("DB_PATH", "bot.db")
TIMEZONE  = os.getenv("TIMEZONE", "Asia/Tashkent")
ONEC_SYNC_URL = os.getenv("ONEC_SYNC_URL")

# ---------- TIMEZONE HELPERS ----------
# zoneinfo доступен с Python 3.9; для 3.8 используем pytz
try:
    from zoneinfo import ZoneInfo
    _HAVE_ZONEINFO = True
except Exception:
    _HAVE_ZONEINFO = False
    import pytz

def now_local_dt() -> datetime:
    if _HAVE_ZONEINFO:
        return datetime.now(ZoneInfo(TIMEZONE))
    else:
        return datetime.now(pytz.timezone(TIMEZONE))

def to_local_hm_from_submitted(s: str) -> str:
    """
    Возвращает HH:MM локального времени из строки submitted_at.
    Поддерживает:
      - "YYYY-MM-DD HH:MM:SS" (уже локальное)
      - "YYYY-MM-DDTHH:MM:SS(.ffffff)" (старое UTC)
    """
    if not s:
        return "—"
    fmts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"]
    dt = None
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return s  # как есть
    if "T" in s:  # считали как UTC → конвертируем в локальную
        if _HAVE_ZONEINFO:
            dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(TIMEZONE))
        else:
            dt = pytz.UTC.localize(dt).astimezone(pytz.timezone(TIMEZONE))
    return dt.strftime("%H:%M")

def submitted_now_str() -> str:
    """Строка локального времени для сохранения в БД: YYYY-MM-DD HH:MM:SS"""
    return datetime.utcnow().isoformat()

def now_local_hm() -> str:
    return now_local_dt().strftime("%H:%M")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- ВАЛИДАЦИЯ ----------
PASSPORT_RE  = re.compile(r"^[A-Z]{2}\d{7}$")
BIRTHDATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")  # dd.mm.yyyy
TIME_RE      = re.compile(r"^\d{2}:\d{2}$")          # HH:MM 24h
CUT_OFF = dtime(8, 10)  # До 08:10 уведомление не требуется

class Reg(StatesGroup):
    passport = State()
    birthdate = State()

class Tardy(StatesGroup):
    waiting_reason = State()
    waiting_start  = State()
    waiting_end    = State()

def normalize_passport(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().upper())

def valid_birthdate(s: str) -> bool:
    if not BIRTHDATE_RE.match(s):
        return False
    try:
        datetime.strptime(s, "%d.%m.%Y")
        return True
    except ValueError:
        return False

def parse_time_hhmm(s: str) -> Optional[dtime]:
    if not TIME_RE.match(s.strip()):
        return None
    try:
        return datetime.strptime(s.strip(), "%H:%M").time()
    except Exception:
        return None

# ---------- Хелперы форматирования уведомлений ----------
def _format_employee_notice(r: dict, emp_name: str, mgr_name: str,
                            status_text: str, status_emoji: str) -> str:
    period = f"{r.get('start_time') or '—'}–{r.get('end_time') or '—'}"
    sent_at = to_local_hm_from_submitted(r.get('submitted_at', ''))
    reason = r.get('reason') or "—"
    return (
        "Запрос на опоздание\n"
        f"Сотрудник: {emp_name}\n"
        f"Руководитель: {mgr_name}\n\n"
        f"Период: {period}\n"
        f"Причина: {reason}\n"
        f"Отправлено: {sent_at}\n\n"
        f"Статус: {status_text} {status_emoji}"
    )

# ---------- КЛАВИАТУРА ----------
def kb_main(is_manager: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="Я опоздал")],  # у всех
        [KeyboardButton(text="🔄 Синхронизация с 1С")],
    ]
    if is_manager:
        rows.insert(0, [KeyboardButton(text="Запросы на опоздание")])  # доп.кнопка только у руководителя
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

# ---------- БАЗА ДАННЫХ ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Пользователи
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id TEXT PRIMARY KEY,
            passport TEXT,
            birthdate TEXT,
            full_name TEXT,
            registered_at TEXT,
            is_manager INTEGER DEFAULT 0,
            supervisor_tg_id TEXT
        )
        """)
        cols = await (await db.execute("PRAGMA table_info(users)")).fetchall()
        existing = {c[1] for c in cols}
        if "is_manager" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN is_manager INTEGER DEFAULT 0")
        if "supervisor_tg_id" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN supervisor_tg_id TEXT")

        # Заявки на опоздание
        await db.execute("""
        CREATE TABLE IF NOT EXISTS tardy_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_tg_id TEXT NOT NULL,
            manager_tg_id  TEXT,
            reason TEXT NOT NULL,
            start_time TEXT,
            end_time   TEXT,
            submitted_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        )
        """)
        cols2 = await (await db.execute("PRAGMA table_info(tardy_requests)")).fetchall()
        existing2 = {c[1] for c in cols2}
        if "start_time" not in existing2:
            await db.execute("ALTER TABLE tardy_requests ADD COLUMN start_time TEXT")
        if "end_time" not in existing2:
            await db.execute("ALTER TABLE tardy_requests ADD COLUMN end_time TEXT")

        await db.commit()

async def get_user(tg_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))).fetchone()
        return dict(row) if row else None

async def upsert_user(
    tg_id: str, passport: str, birthdate: str, full_name: str,
    is_manager: bool = False, supervisor_tg_id: Optional[str] = None
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO users (tg_id, passport, birthdate, full_name, registered_at, is_manager, supervisor_tg_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET
            passport=excluded.passport,
            birthdate=excluded.birthdate,
            full_name=excluded.full_name,
            registered_at=excluded.registered_at,
            is_manager=excluded.is_manager,
            supervisor_tg_id=excluded.supervisor_tg_id
        """, (tg_id, passport, birthdate, full_name, now_local_dt().strftime("%Y-%m-%d %H:%M:%S"),
              1 if is_manager else 0, supervisor_tg_id))
        await db.commit()

async def delete_user(tg_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
        await db.commit()

async def create_tardy_request(employee_tg_id: str, manager_tg_id: str, reason: str,
                               start_hm: Optional[str], end_hm: Optional[str]) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
        INSERT INTO tardy_requests (employee_tg_id, manager_tg_id, reason, start_time, end_time, submitted_at, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (employee_tg_id, manager_tg_id, reason, start_hm, end_hm, submitted_now_str()))
        await db.commit()
        return cur.lastrowid

async def set_user_supervisor(tg_id: str, supervisor_tg_id: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET supervisor_tg_id = ? WHERE tg_id = ?",
            (supervisor_tg_id, tg_id)
        )
        await db.commit()


async def get_pending_tardy_for_manager(manager_tg_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("""
            SELECT * FROM tardy_requests
            WHERE manager_tg_id = ? AND status = 'pending'
            ORDER BY submitted_at DESC
        """, (manager_tg_id,))).fetchall()
        return [dict(r) for r in rows]

async def get_tardy(req_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM tardy_requests WHERE id = ?", (req_id,))).fetchone()
        return dict(row) if row else None

async def set_tardy_status(req_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tardy_requests SET status = ? WHERE id = ?", (status, req_id))
        await db.commit()

# ---------- ВЗАИМОДЕЙСТВИЕ С 1С ----------
def _make_auth():
    return aiohttp.BasicAuth(ONEC_USER, ONEC_PASS) if ONEC_USER and ONEC_PASS else None

async def register_in_1c(passport: str, birthdate: str, user_id: str) -> tuple[int, dict, str]:
    """POST на 1С при регистрации. Возвращает (status, json, raw_text)."""
    payload = {"passport": passport, "birthdate": birthdate, "user_id": user_id}
    auth = _make_auth()
    async with aiohttp.ClientSession(auth=auth) as session:
        async with session.post(ONEC_URL, json=payload) as resp:
            status = resp.status
            txt = await resp.text()
            try:
                js = await resp.json(content_type=None)
            except Exception:
                js = {}
            return status, js, txt

async def sync_supervisor_from_1c(user_id: str) -> Optional[str]:
    """
    Делает POST на ONEC_SYNC_URL с паспортом и датой рождения пользователя.
    Ожидает в ответе id начальника и сохраняет его в users.supervisor_tg_id.
    Возвращает сохранённый supervisor_tg_id либо None.
    """
    user = await get_user(user_id)
    if not user:
        return None
    passport = user.get("passport")
    birthdate = user.get("birthdate")
    if not passport or not birthdate:
        return None

    payload = {"passport": passport, "birthdate": birthdate}
    auth = _make_auth()
    async with aiohttp.ClientSession(auth=auth) as session:
        async with session.post(ONEC_SYNC_URL, json=payload) as resp:
            txt = await resp.text()
            try:
                js = await resp.json(content_type=None)
            except Exception:
                js = {}

    # Пытаемся вытащить ID начальника из возможных ключей или просто из цифр
    raw_id = (
        js.get("supervisor_tg_id")
        or js.get("manager_tg_id")
        or js.get("managerId")
        or js.get("supervisorId")
        or js.get("supervisor")
        or js.get("manager")
        or ""
    )
    # извлечём цифры на случай форматов вида "tg://user?id=123456789"
    m = re.search(r"\d{5,}", str(raw_id))
    supervisor_tg_id = m.group(0) if m else None

    await set_user_supervisor(user_id, supervisor_tg_id)
    return supervisor_tg_id


# ----- NEW: отправка решения руководителя в 1С -----
async def send_decision_to_1c(r: dict, decision: str, emp_name: str, mgr_name: str):
    """
    r — запись из tardy_requests (dict)
    decision — 'approved' | 'rejected'
    """
    if not ONEC_DECISION_URL:
        return
    payload = {
        "req_id": r["id"],
        "employee_tg_id": str(r["employee_tg_id"]),
        "manager_tg_id":  str(r["manager_tg_id"]),
        "employee_name":  emp_name,
        "manager_name":   mgr_name,
        "reason":         r.get("reason"),
        "start":          r.get("start_time"),     # "HH:MM"
        "end":            r.get("end_time"),       # "HH:MM"
        "submitted_at":   r.get("submitted_at"),   # локальная "YYYY-MM-DD HH:MM:SS" (или старые ISO)
        "decided_at":     now_local_dt().strftime("%Y-%m-%d %H:%M:%S"),
        "decision":       decision                  # 'approved' | 'rejected'
    }
    auth = _make_auth()
    try:
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.post(ONEC_DECISION_URL, json=payload) as resp:
                _ = await resp.text()  # можно залогировать при необходимости
    except Exception:
        # не роняем UX, просто молча пропускаем (или лог)
        pass

async def refresh_from_1c_by_tg(user_id: str) -> Optional[dict]:
    user = await get_user(user_id)
    if not user:
        return None
    status, js, _ = await register_in_1c(user["passport"], user["birthdate"], user_id)
    if status == 200:
        name = js.get("fullName") or js.get("name") or user.get("full_name") or "сотрудник"
        is_manager = bool(js.get("isManager") or js.get("is_manager") or js.get("Руководитель") or False)
        supervisor_tg_id = js.get("supervisor_tg_id") or js.get("manager_tg_id") or user.get("supervisor_tg_id")
        await upsert_user(user_id, user["passport"], user["birthdate"], name, is_manager, supervisor_tg_id)
        return {"fullName": name, "isManager": is_manager, "supervisor_tg_id": supervisor_tg_id}
    return None

# ---------- ХЭНДЛЕРЫ ----------
@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user = await get_user(user_id)
    if user:
        await state.clear()
        name = user.get("full_name") or "сотрудник"
        is_mgr = bool(user.get("is_manager"))
        await message.answer(f"✅ Вы уже зарегистрированы как: {name}", reply_markup=kb_main(is_mgr))
        return

    await state.clear()
    await state.set_state(Reg.passport)
    await message.answer("Введите серию и номер паспорта (пример: AD1234567).")

@dp.message(Command("reset"))
async def reset_registration(message: Message, state: FSMContext):
    user_id = str(message.from_user.id)
    await delete_user(user_id)
    await state.clear()
    await state.set_state(Reg.passport)
    await message.answer("🔁 Регистрация сброшена.\nВведите серию и номер паспорта (пример: AD1234567).")

@dp.message(Command("refresh"))
async def refresh_user(message: Message):
    user_id = str(message.from_user.id)
    updated = await refresh_from_1c_by_tg(user_id)
    if updated:
        await message.answer(
            f"🔄 Данные обновлены из 1С.\n"
            f"Сотрудник: {updated.get('fullName')}\n"
            f"Руководитель: {'да' if updated.get('isManager') else 'нет'}",
            reply_markup=kb_main(bool(updated.get("isManager")))
        )
    else:
        await message.answer("⚠️ Не удалось обновить данные из 1С. Возможно, вы не зарегистрированы или 1С вернула не 200.")

@dp.message(F.text == "🔄 Синхронизация с 1С")
async def sync_with_1c(message: Message):
    user_id = str(message.from_user.id)
    user = await get_user(user_id)
    if not user:
        await message.answer("Сначала нужно зарегистрироваться: /start")
        return

    await message.answer("🔄 Синхронизирую с 1С…")
    try:
        supervisor_id = await sync_supervisor_from_1c(user_id)
        if supervisor_id:
            await message.answer(
                f"✅ Синхронизация завершена.\nРуководитель (TG ID): {supervisor_id}",
                reply_markup=kb_main(bool(user.get("is_manager")))
            )
        else:
            await message.answer("⚠️ 1С не вернула корректный ID руководителя.")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка синхронизации: {e}")



@dp.message(Command("whoami"))
async def whoami(message: Message):
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")

@dp.message(Reg.passport, F.text)
async def ask_birthdate(message: Message, state: FSMContext):
    p = normalize_passport(message.text)
    if not PASSPORT_RE.match(p):
        await message.answer("❌ Неверный формат. Пример: AD1234567 (2 буквы + 7 цифр). Попробуйте ещё раз.")
        return
    await state.update_data(passport=p)
    await state.set_state(Reg.birthdate)
    await message.answer("Введите дату рождения (пример: 30.09.2005).")

@dp.message(Reg.birthdate, F.text)
async def do_register(message: Message, state: FSMContext):
    b = message.text.strip()
    if not valid_birthdate(b):
        await message.answer("❌ Неверный формат даты. Нужен дд.мм.гггг (например: 30.09.2005).")
        return

    data = await state.get_data()
    passport = data["passport"]
    user_id = str(message.from_user.id)

    await message.answer("🔄 Выполняется синхронизация с 1С…")
    try:
        status, js, raw = await register_in_1c(passport, b, user_id)
        if status == 200:
            name = js.get("fullName") or js.get("name") or "сотрудник"
            is_manager = bool(js.get("isManager") or js.get("is_manager") or js.get("Руководитель") or False)
            supervisor_tg_id = js.get("supervisor_tg_id") or js.get("manager_tg_id")

            await upsert_user(user_id, passport, b, name, is_manager, supervisor_tg_id)
            await message.answer(f"✅ Регистрация успешна\n👤 Сотрудник: {name}", reply_markup=kb_main(is_manager))
            await state.clear()

        elif status in (404, 204):
            await message.answer("❌ Сотрудник с такими данными не найден.")
        elif status == 409:
            await message.answer("⚠️ Найдено несколько сотрудников (дубликаты). Обратитесь к администратору.")
        elif status == 400:
            reason = js.get("reason") or "Неверные данные."
            await message.answer(f"⚠️ Ошибка проверки: {reason}")
        else:
            await message.answer(f"⚠️ Неожиданный ответ 1С ({status}): {raw[:500]}")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка соединения с 1С: {e}")

# ---- «Я опоздал» (для всех) ----
@dp.message(F.text == "Я опоздал")
async def tardy_start(message: Message, state: FSMContext):
    user_id = str(message.from_user.id)
    user = await get_user(user_id)
    if not user:
        await message.answer("Сначала нужно зарегистрироваться: /start")
        return

    now_t = now_local_dt().time()
    if now_t <= CUT_OFF:
        await message.answer("⏱ Опоздание в пределах допустимого. Уведомление не требуется.")
        return

    await state.set_state(Tardy.waiting_reason)
    await message.answer("Укажите причину опоздания одним сообщением:")

@dp.message(Tardy.waiting_reason, F.text)
async def tardy_reason(message: Message, state: FSMContext):
    reason = message.text.strip()
    if not reason:
        await message.answer("Причина не может быть пустой. Укажите причину опоздания:")
        return
    await state.update_data(tardy_reason=reason)
    await state.set_state(Tardy.waiting_start)
    await message.answer("Укажите *время начала* опоздания в формате HH:MM (например, 09:20):", parse_mode=None)

@dp.message(Tardy.waiting_start, F.text)
async def tardy_start_time(message: Message, state: FSMContext):
    s = message.text.strip()
    t = parse_time_hhmm(s)
    if not t:
        await message.answer("⏱ Неверный формат. Введите время начала в формате HH:MM (например, 09:20):")
        return
    await state.update_data(tardy_start=s)
    await state.set_state(Tardy.waiting_end)
    await message.answer("Теперь укажите *время конца* опоздания в формате HH:MM (например, 09:45):", parse_mode=None)

@dp.message(Tardy.waiting_end, F.text)
async def tardy_end_time(message: Message, state: FSMContext):
    e_str = message.text.strip()
    e_time = parse_time_hhmm(e_str)
    if not e_time:
        await message.answer("⏱ Неверный формат. Введите время конца в формате HH:MM (например, 09:45):")
        return

    data = await state.get_data()
    start_str = data.get("tardy_start")
    reason = data.get("tardy_reason")
    user_id = str(message.from_user.id)
    user = await get_user(user_id)

    # финальная валидация
    s_time = parse_time_hhmm(start_str) if start_str else None
    if not s_time:
        await message.answer("Не удалось прочитать время начала. Попробуйте заново: «Я опоздал».")
        await state.clear()
        return

    if e_time < s_time:
        await message.answer("Время конца не может быть раньше начала. Введите время конца ещё раз (HH:MM):")
        return

    if not user:
        await message.answer("Сначала зарегистрируйтесь: /start")
        await state.clear()
        return

    manager_tg_id = str(user.get("supervisor_tg_id") or "").strip()
    if not manager_tg_id.isdigit():
        await message.answer("В системе не указан корректный Telegram ID руководителя.")
        await state.clear()
        return

    # сохраняем заявку
    req_id = await create_tardy_request(user_id, manager_tg_id, reason, start_str, e_str)

    # уведомляем руководителя
    text_for_manager = (
        "Новый запрос на опоздание\n"
        f"Сотрудник: {user.get('full_name')}\n"
        f"Период: {start_str}–{e_str}\n"
        f"Причина: {reason}\n"
        f"Отправлено: {now_local_hm()}"
    )
    ikb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"tardy_ok:{req_id}"),
        InlineKeyboardButton(text="❌ Отказать",  callback_data=f"tardy_rej:{req_id}")
    ]])

    try:
        await bot.send_message(int(manager_tg_id), text_for_manager, reply_markup=ikb)
        await message.answer("✅ Запрос отправлен руководителю.")
    except Exception:
        await message.answer("⚠️ Не удалось отправить руководителю (возможно, он ещё не зарегистрирован в боте).")
    finally:
        await state.clear()

# ---- «Запросы на опоздание» (для руководителей) ----
@dp.message(F.text == "Запросы на опоздание")
async def tardy_list(message: Message):
    manager_id = str(message.from_user.id)
    me = await get_user(manager_id)
    if not me or not me.get("is_manager"):
        await message.answer("Эта функция доступна только руководителям.")
        return

    pending = await get_pending_tardy_for_manager(manager_id)
    if not pending:
        await message.answer("Нет новых запросов.")
        return

    for r in pending:
        emp = await get_user(r["employee_tg_id"])
        emp_name = emp.get('full_name') if emp else r["employee_tg_id"]
        local_hm = to_local_hm_from_submitted(r.get('submitted_at'))
        period = f"{r.get('start_time') or '—'}–{r.get('end_time') or '—'}"
        text = (
            f"Запрос #{r['id']}\n"
            f"Сотрудник: {emp_name}\n"
            f"Период: {period}\n"
            f"Причина: {r['reason']}\n"
            f"Отправлено: {local_hm}"
        )
        ikb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"tardy_ok:{r['id']}"),
            InlineKeyboardButton(text="❌ Отказать",  callback_data=f"tardy_rej:{r['id']}")
        ]])
        await message.answer(text, reply_markup=ikb)

# ---- Обработка нажатий Одобрить/Отказать ----
@dp.callback_query(F.data.startswith("tardy_ok:"))
async def tardy_approve(cb: CallbackQuery):
    req_id = int(cb.data.split(":")[1])
    r = await get_tardy(req_id)
    if not r or r["status"] != "pending":
        await cb.answer("Заявка не найдена или уже обработана", show_alert=True)
        return
    if str(cb.from_user.id) != str(r["manager_tg_id"]):
        await cb.answer("Недостаточно прав для этой заявки", show_alert=True)
        return

    await set_tardy_status(req_id, "approved")
    await cb.message.edit_text(cb.message.text + "\n\nСтатус: ✅ Одобрено")

    # уведомление сотруднику (подробное)
    emp = await get_user(r["employee_tg_id"])
    mgr = await get_user(r["manager_tg_id"])
    emp_name = (emp or {}).get("full_name") or str(r["employee_tg_id"])
    mgr_name = (mgr or {}).get("full_name") or str(r["manager_tg_id"])
    msg_emp = _format_employee_notice(r, emp_name, mgr_name, "Одобрено", "✅")
    try:
        await bot.send_message(r["employee_tg_id"], msg_emp)
    except Exception:
        pass

    # запись решения в 1С
    await send_decision_to_1c(r, "approved", emp_name, mgr_name)
    await cb.answer("Одобрено")

@dp.callback_query(F.data.startswith("tardy_rej:"))
async def tardy_reject(cb: CallbackQuery):
    req_id = int(cb.data.split(":")[1])
    r = await get_tardy(req_id)
    if not r or r["status"] != "pending":
        await cb.answer("Заявка не найдена или уже обработана", show_alert=True)
        return
    if str(cb.from_user.id) != str(r["manager_tg_id"]):
        await cb.answer("Недостаточно прав для этой заявки", show_alert=True)
        return

    await set_tardy_status(req_id, "rejected")
    await cb.message.edit_text(cb.message.text + "\n\nСтатус: ❌ Отклонено")

    # уведомление сотруднику (подробное)
    emp = await get_user(r["employee_tg_id"])
    mgr = await get_user(r["manager_tg_id"])
    emp_name = (emp or {}).get("full_name") or str(r["employee_tg_id"])
    mgr_name = (mgr or {}).get("full_name") or str(r["manager_tg_id"])
    msg_emp = _format_employee_notice(r, emp_name, mgr_name, "Отказано", "❌")
    try:
        await bot.send_message(r["employee_tg_id"], msg_emp)
    except Exception:
        pass

    # запись решения в 1С
    await send_decision_to_1c(r, "rejected", emp_name, mgr_name)
    await cb.answer("Отклонено")

# ---------- MAIN ----------
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
