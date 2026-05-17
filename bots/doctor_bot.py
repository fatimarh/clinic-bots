# bots/doctor_bot.py
import asyncio
import re
from pathlib import Path
from datetime import datetime, date

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from common.config import get_doctor_token
from common.logging_config import setup_logger
from common.db import (
    migrate,
    get_doctor_by_tg,
    upsert_doctor,
    update_doctor_name,
    get_doctor_id_by_tg,
    get_or_create_patient,
    add_referral,
    list_referrals_by_doctor,
    list_years_with_referrals,
    list_months_for_year,
    delete_referrals,
    update_doctor_profile,
)

# --- logger ---
ROOT = Path(__file__).resolve().parents[1]
log = setup_logger("doctor_bot", ROOT / "logs" / "doctor.log")

# --- router ---
router = Router()

# =========================
# Validation helpers

#  Phone validation --- проверяем на корректность формата +7XXXXXXXXXX
PHONE_RE = re.compile(r"^\+?\d[\d\s\-\(\)]{9,18}$")

def normalize_phone(text: str) -> str | None:
    s = (text or "").strip()
    if not PHONE_RE.match(s):
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) < 10 or len(digits) > 12:
        return None
    if digits.startswith("7"):
        return "+" + digits
    return "+" + digits
# =========================
def doctor_profile_complete(doctor: dict | None) -> bool:
    if not doctor:
        return False
    return all([
        (doctor.get("phone") or "").strip(),
        (doctor.get("specialization") or "").strip(),
        (doctor.get("workplace") or "").strip(),
        (doctor.get("city") or "").strip(),
    ])

# Разрешаем кириллицу/латиницу, пробел, дефис, апостроф (оба варианта)
NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁёІіЇїЄє'’\- ]{3,80}$")

def is_valid_full_name(text: str) -> bool:
    return bool(text and NAME_RE.match(text.strip()))

def parse_birth_date_ru(text: str) -> date | None:
    """Ожидаем ДД.ММ.ГГГГ, реальная дата от 1900 до сегодня."""
    s = (text or "").strip()
    try:
        d = datetime.strptime(s, "%d.%m.%Y").date()
    except Exception:
        return None
    if d.year < 1900 or d > date.today():
        return None
    return d

def fmt_iso_to_ru(iso_ts: str | None) -> str:
    """Для created_at SQLite: 'YYYY-MM-DD HH:MM:SS' -> 'ДД.ММ.ГГГГ'."""
    if not iso_ts:
        return "???.???.????"
    try:
        dt = datetime.fromisoformat(iso_ts)
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return iso_ts

def fmt_birth_ru(birth_iso: str | None) -> str:
    """Для birth_date: 'YYYY-MM-DD' -> 'ДД.ММ.ГГГГ'."""
    if not birth_iso:
        return "???.???.????"
    try:
        return datetime.strptime(birth_iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return birth_iso

# =========================
# UI helpers
# =========================
def fmt_help(initialized: bool) -> str:
    base = [
        "/start — начать",
        "/help — помощь",
    ]
    if initialized:
        base += [
            "/add_patient — добавить пациента",
            "/patients — просмотр списков",
            "/profile — ваш профиль и изменение данных",
        ]
    else:
        base += ["Сначала укажите ФИО — бот запросит его автоматически."]
    return "\n".join(base)

def fmt_profile(doctor: dict) -> str:
    return (
        "👤 <b>Ваш профиль</b>\n\n"
        f"ФИО: <b>{doctor.get('full_name') or '—'}</b>\n"
        f"Телефон: <b>{doctor.get('phone') or '—'}</b>\n"
        f"Специализация: <b>{doctor.get('specialization') or '—'}</b>\n"
        f"Место работы: <b>{doctor.get('workplace') or '—'}</b>\n"
        f"Город: <b>{doctor.get('city') or '—'}</b>\n"
    )

def kb_profile() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏ Изменить данные", callback_data="profile:edit")
    kb.button(text="⬅️ Назад", callback_data="profile:back")
    kb.adjust(1)
    return kb

def kb_profile_fields() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="ФИО", callback_data="profile:field:full_name")
    kb.button(text="Телефон", callback_data="profile:field:phone")
    kb.button(text="Специализация", callback_data="profile:field:specialization")
    kb.button(text="Место работы", callback_data="profile:field:workplace")
    kb.button(text="Город", callback_data="profile:field:city")
    kb.button(text="⬅️ Назад", callback_data="profile:back")
    kb.adjust(1)
    return kb

def build_quick_actions() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить пациента", callback_data="qa:add_patient")
    kb.button(text="📋 Списки пациентов", callback_data="qa:patients_menu")
    kb.button(text="🗑 Удалить направление", callback_data="qa:delete_menu")
    kb.button(text="✅ Рассчитанные", callback_data="patients:settled")
    kb.button(text="⏳ Не рассчитанные", callback_data="patients:unsettled")
    kb.button(text="👤 Ваш профиль", callback_data="qa:profile")
    kb.button(text="ℹ️ Полная справка", callback_data="qa:help")
    kb.adjust(1, 1, 1, 2, 1, 1)
    return kb

async def send_quick_actions(target: Message | CallbackQuery):
    message_obj = target if isinstance(target, Message) else target.message
    await message_obj.answer(
        "Выберите следующее действие:",
        reply_markup=build_quick_actions().as_markup()
    )

def build_patients_menu() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Весь список", callback_data="patients:all")
    kb.button(text="Текущий месяц", callback_data="patients:cur_month")
    kb.button(text="Выбрать год и месяц", callback_data="patients:pick_year")
    kb.adjust(1)
    return kb

def build_delete_menu() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="Удалить из всего списка", callback_data="del:all")
    kb.button(text="Удалить за текущий месяц", callback_data="del:cur_month")
    kb.button(text="Удалить за год и месяц", callback_data="del:pick_year")
    kb.button(text="⬅️ Назад", callback_data="qa:patients_menu")
    kb.adjust(1)
    return kb

async def send_referral_list_numbered(
    msg: Message,
    items: list[dict],
    title: str,
    show_settled_info: bool = False,
    filter_settled: int | None = None,
):
    """Печать нумерованного списка направлений, с опциями."""
    if filter_settled is not None:
        items = [r for r in items if int(r.get("settled", 0) or 0) == int(filter_settled)]

    if not items:
        await msg.answer(f"{title}\nСписок пуст.")
        await send_quick_actions(msg)
        return

    lines: list[str] = []
    for i, r in enumerate(items, 1):
        fio = r["patient_full_name"]
        bd_ru = fmt_birth_ru(r.get("patient_birth_date"))
        d_added = fmt_iso_to_ru(r.get("created_at"))
        line = f"{i}. {fio} {bd_ru} - дата добавления {d_added}"
        if show_settled_info and int(r.get("settled", 0) or 0) == 1:
            settled_at = fmt_iso_to_ru(r.get("settled_at"))
            line += f" — Рассчитан: {settled_at}"
        lines.append(line)

    # Разбивка на чанки < 4096
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await msg.answer(f"{title}\n{chunk.strip()}")
            chunk = ""
        chunk += line + "\n"
    if chunk:
        chunk = chunk.strip() + f"\nИтого: {len(lines)}"
        await msg.answer(f"{title}\n{chunk}")

    await send_quick_actions(msg)

# =========================
# FSM
# =========================
class Onboarding(StatesGroup):
    waiting_full_name = State()

class DoctorProfile(StatesGroup):
    waiting_phone = State()
    waiting_specialization = State()
    waiting_workplace = State()
    waiting_city = State()

class EditName(StatesGroup):
    waiting_new_full_name = State()

class EditProfile(StatesGroup):
    waiting_field = State()
    waiting_value = State()

class AddPatient(StatesGroup):
    waiting_patient_full_name = State()
    waiting_patient_birth_date = State()

class DeleteReferral(StatesGroup):
    waiting_numbers = State()
    confirm = State()

# =========================
# Handlers: onboarding/help
# =========================
@router.message(CommandStart())
async def start_handler(msg: Message, state: FSMContext):
    tg_user_id = msg.from_user.id
    doctor = get_doctor_by_tg(tg_user_id)

    # Если врача нет — сначала ФИО
    if doctor is None:
        await msg.answer("Здравствуйте! Для начала отправьте ваши ФИО одной строкой (например: Иванов Иван Иванович).")
        await state.set_state(Onboarding.waiting_full_name)
        return

    # Если профиль не заполнен — запускаем профиль
    if not doctor_profile_complete(doctor):
        await msg.answer(
            "✅ Авторизация успешна.\n"
            "Нужно заполнить профиль врача.\n\n"
            "Введите номер телефона (обязательно):"
        )
        await state.set_state(DoctorProfile.waiting_phone)
        return

    # Всё заполнено — обычный режим
    await msg.answer("✅ Авторизация успешна.\nℹ️ Полное меню — команда /help")
    await send_quick_actions(msg)


@router.message(Command("help"))
async def help_handler(msg: Message):
    tg_user_id = msg.from_user.id
    doctor = get_doctor_by_tg(tg_user_id)
    await msg.answer(fmt_help(initialized=(doctor is not None)))
    if doctor is not None:
        await send_quick_actions(msg)

@router.message(Command("whoami"))
async def whoami_handler(msg: Message):
    tg_user_id = msg.from_user.id
    doctor = get_doctor_by_tg(tg_user_id)
    if doctor is None:
        await msg.answer("Вы ещё не зарегистрированы. Отправьте ваши ФИО одной строкой.")
        return
    await msg.answer(f"Ваше ФИО: {doctor['full_name']}")
    await send_quick_actions(msg)

@router.message(Command("edit_name"))
async def edit_name_start(msg: Message, state: FSMContext):
    tg_user_id = msg.from_user.id
    doctor = get_doctor_by_tg(tg_user_id)
    if doctor is None:
        await msg.answer("Вы ещё не зарегистрированы. Отправьте ваши ФИО одной строкой.")
        await state.set_state(Onboarding.waiting_full_name)
        return
    await msg.answer("Отправьте новые ФИО одной строкой (например: «Иванов Иван Иванович»).")
    await state.set_state(EditName.waiting_new_full_name)

@router.message(Command("profile"))
async def profile_cmd(msg: Message):
    tg_user_id = msg.from_user.id
    doctor = get_doctor_by_tg(tg_user_id)
    if doctor is None:
        await msg.answer("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        return
    await msg.answer(fmt_profile(doctor), reply_markup=kb_profile().as_markup())
    # меню не показываем автоматически — только профиль

@router.callback_query(F.data == "qa:profile")
async def qa_profile(cb: CallbackQuery):
    tg_user_id = cb.from_user.id
    doctor = get_doctor_by_tg(tg_user_id)
    if doctor is None:
        await cb.message.answer("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        await cb.answer()
        return
    await cb.message.answer(fmt_profile(doctor), reply_markup=kb_profile().as_markup())
    await cb.answer()

@router.callback_query(F.data == "profile:back")
async def profile_back(cb: CallbackQuery):
    await cb.answer()
    await send_quick_actions(cb)

@router.callback_query(F.data == "profile:edit")
async def profile_edit(cb: CallbackQuery, state: FSMContext):
    tg_user_id = cb.from_user.id
    doctor = get_doctor_by_tg(tg_user_id)
    if doctor is None:
        await cb.message.answer("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        await cb.answer()
        return
    await state.clear()
    await cb.message.answer("Что хотите изменить?", reply_markup=kb_profile_fields().as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("profile:field:"))
async def profile_pick_field(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[-1]
    await state.update_data(profile_field=field)
    await state.set_state(EditProfile.waiting_value)

    prompts = {
        "full_name": "Введите новое ФИО одной строкой:",
        "phone": "Введите новый номер телефона (пример: +79001234567 или 89001234567):",
        "specialization": "Введите новую специализацию:",
        "workplace": "Введите новое место работы:",
        "city": "Введите новый город:",
    }
    await cb.message.answer(prompts.get(field, "Введите новое значение:"))
    await cb.answer()

@router.message(EditProfile.waiting_value)
async def profile_apply_value(msg: Message, state: FSMContext):
    tg_user_id = msg.from_user.id
    doctor = get_doctor_by_tg(tg_user_id)
    if doctor is None:
        await state.clear()
        await msg.answer("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        return

    data = await state.get_data()
    field = data.get("profile_field")
    value = (msg.text or "").strip()

    # валидируем
    if field == "full_name":
        if not is_valid_full_name(value):
            await msg.answer("Некорректное ФИО. Попробуйте ещё раз:")
            return
        update_doctor_name(tg_user_id, value)

    elif field == "phone":
        phone = normalize_phone(value)
        if not phone:
            await msg.answer("❌ Некорректный номер. Пример: +79001234567 или 89001234567. Попробуйте ещё раз:")
            return
        # обновляем через update_doctor_profile, сохраняя остальные поля
        update_doctor_profile(
            tg_user_id=tg_user_id,
            phone=phone,
            specialization=doctor.get("specialization") or "",
            workplace=doctor.get("workplace") or "",
            city=doctor.get("city") or "",
        )

    elif field in ("specialization", "workplace", "city"):
        if not value:
            await msg.answer("Поле не может быть пустым. Попробуйте ещё раз:")
            return
        # обновляем через update_doctor_profile, сохраняя остальные поля
        update_doctor_profile(
            tg_user_id=tg_user_id,
            phone=doctor.get("phone") or "",
            specialization=value if field == "specialization" else (doctor.get("specialization") or ""),
            workplace=value if field == "workplace" else (doctor.get("workplace") or ""),
            city=value if field == "city" else (doctor.get("city") or ""),
        )
    else:
        await msg.answer("Неизвестное поле.")
        await state.clear()
        return

    await state.clear()
    doctor2 = get_doctor_by_tg(tg_user_id)
    await msg.answer("✅ Данные обновлены.")
    await msg.answer(fmt_profile(doctor2), reply_markup=kb_profile().as_markup())

@router.message(Onboarding.waiting_full_name)
async def onboarding_full_name(msg: Message, state: FSMContext):
    full_name = (msg.text or "").strip()
    if not is_valid_full_name(full_name):
        await msg.answer(
            "Некорректные ФИО. Разрешены буквы, пробел, дефис, апостроф. "
            "Минимум 3 символа. Пример: «Иванов Иван Иванович».\n"
            "Попробуйте ещё раз:"
        )
        return
 
    tg_user_id = msg.from_user.id
    upsert_doctor(tg_user_id, full_name)

    await state.clear()
    await msg.answer("Готово. Теперь нужно заполнить профиль.\n\nВведите номер телефона (обязательно):")
    await state.set_state(DoctorProfile.waiting_phone)
    log.info(f"Doctor registered tg_user_id={tg_user_id} full_name={full_name}")

@router.message(DoctorProfile.waiting_phone)
async def profile_phone(msg: Message, state: FSMContext):
    phone = normalize_phone(msg.text)
    if not phone:
            await msg.answer("❌ Некорректный номер. Пример: +79001234567 или 89001234567. Попробуйте ещё раз:")
            return
    await state.update_data(phone=phone)
    await msg.answer("Ваша специализация (например: терапевт, ортопед, хирург):")
    await state.set_state(DoctorProfile.waiting_specialization)

@router.message(DoctorProfile.waiting_specialization)
async def profile_specialization(msg: Message, state: FSMContext):
    spec = (msg.text or "").strip()
    if not spec:
        await msg.answer("Поле не может быть пустым. Введите вашу специализацию:")
        return
    await state.update_data(specialization=spec)
    await msg.answer("Место работы (например: Клиника Здоровья, Поликлиника №...):")
    await state.set_state(DoctorProfile.waiting_workplace)

@router.message(DoctorProfile.waiting_workplace)
async def profile_workplace(msg: Message, state: FSMContext):
    workplace = (msg.text or "").strip()
    if not workplace:
        await msg.answer("Поле не может быть пустым. Введите место работы:")
        return
    await state.update_data(workplace=workplace)
    await msg.answer("Город:")
    await state.set_state(DoctorProfile.waiting_city)

@router.message(DoctorProfile.waiting_city)
async def profile_city(msg: Message, state: FSMContext):
    city = (msg.text or "").strip()
    if not city:
        await msg.answer("Поле не может быть пустым. Введите город:")
        return

    data = await state.get_data()
    phone = data["phone"]
    specialization = data["specialization"]
    workplace = data["workplace"]

    tg_user_id = msg.from_user.id
    update_doctor_profile(
        tg_user_id=tg_user_id,
        phone=phone,
        specialization=specialization,
        workplace=workplace,
        city=city,
    )

    await state.clear()
    await msg.answer("✅ Профиль заполнен.\nℹ️ Полное меню — команда /help")
    await send_quick_actions(msg)

    log.info(f"Doctor profile updated tg_user_id={tg_user_id} phone={phone} city={city}")

@router.message(EditName.waiting_new_full_name)
async def edit_name_apply(msg: Message, state: FSMContext):
    full_name = (msg.text or "").strip()
    if not is_valid_full_name(full_name):
        await msg.answer(
            "Некорректные ФИО. Разрешены буквы, пробел, дефис, апостроф. "
            "Минимум 3 символа. Пример: «Иванов Иван Иванович».\n"
            "Попробуйте ещё раз:"
        )
        return
    tg_user_id = msg.from_user.id
    update_doctor_name(tg_user_id, full_name)
    await state.clear()
    await msg.answer(f"ФИО обновлены: {full_name}")
    await send_quick_actions(msg)
    log.info(f"Doctor name updated tg_user_id={tg_user_id} full_name={full_name}")

# =========================
# Add patient (FSM)
# =========================
@router.message(Command("add_patient"))
async def add_patient_start(msg: Message, state: FSMContext):
    tg_user_id = msg.from_user.id
    doctor = get_doctor_by_tg(tg_user_id)
    if doctor is None:
        await msg.answer("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        await state.set_state(Onboarding.waiting_full_name)
        return
    await msg.answer("Введите ФИО пациента (одной строкой).")
    await state.set_state(AddPatient.waiting_patient_full_name)

@router.message(AddPatient.waiting_patient_full_name)
async def add_patient_full_name(msg: Message, state: FSMContext):
    full_name = (msg.text or "").strip()
    if not is_valid_full_name(full_name):
        await msg.answer(
            "Некорректные ФИО пациента. Разрешены буквы, пробел, дефис, апостроф. "
            "Минимум 3 символа. Пример: «Петров Пётр Петрович».\n"
            "Попробуйте ещё раз:"
        )
        return
    await state.update_data(patient_full_name=full_name)
    await msg.answer("Введите дату рождения пациента в формате ДД.ММ.ГГГГ (например: 07.04.2018).")
    await state.set_state(AddPatient.waiting_patient_birth_date)

@router.message(AddPatient.waiting_patient_birth_date)
async def add_patient_birth_date(msg: Message, state: FSMContext):
    d = parse_birth_date_ru(msg.text or "")
    if not d:
        await msg.answer(
            "Некорректная дата. Введите в формате ДД.ММ.ГГГГ и реальную дату (с 1900 года по сегодня).\n"
            "Пример: 31.12.2008\n"
            "Попробуйте ещё раз:"
        )
        return

    data = await state.get_data()
    patient_full_name = data["patient_full_name"]
    birth_iso = d.strftime("%Y-%m-%d")

    tg_user_id = msg.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    if doctor_id is None:
        await msg.answer("Ошибка: ваш профиль врача не найден. Повторите /start.")
        await state.clear()
        return

    patient_id = get_or_create_patient(patient_full_name, birth_iso)
    referral_id = add_referral(doctor_id, patient_id)
    await state.clear()

    await msg.answer("Пациент записан.")
    await send_quick_actions(msg)
    log.info(f"Referral added id={referral_id} doctor_id={doctor_id} patient='{patient_full_name}' {birth_iso}")

# =========================
# Patients list (inline)
# =========================
@router.message(Command("patients"))
async def patients_menu(msg: Message):
    tg_user_id = msg.from_user.id
    doctor = get_doctor_by_tg(tg_user_id)
    if doctor is None:
        await msg.answer("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        return
    await msg.answer("Выберите режим:", reply_markup=build_patients_menu().as_markup())

async def _send_list_and_quick(msg: Message, items: list[dict], title: str):
    await send_referral_list_numbered(msg, items, title)

@router.callback_query(F.data == "patients:all")
async def cb_patients_all(cb: CallbackQuery):
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    if doctor_id is None:
        await cb.message.edit_text("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        await cb.answer()
        return
    items = list_referrals_by_doctor(doctor_id)
    await _send_list_and_quick(cb.message, items, "Все пациенты")
    await cb.answer()

@router.callback_query(F.data == "patients:cur_month")
async def cb_patients_cur_month(cb: CallbackQuery):
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    if doctor_id is None:
        await cb.message.edit_text("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        await cb.answer()
        return
    today = date.today()
    items = list_referrals_by_doctor(doctor_id, year=today.year, month=today.month)
    await _send_list_and_quick(cb.message, items, f"Пациенты за {today.month:02d}.{today.year}")
    await cb.answer()

# ---- Выбор года/месяца ----
def build_years_kb(years: list[int]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    if not years:
        kb.button(text="(Нет данных)", callback_data="patients:nodata")
    else:
        for y in years:
            kb.button(text=str(y), callback_data=f"patients:year:{y}")
        kb.adjust(4)
    kb.button(text="⬅️ Назад", callback_data="patients:back")
    return kb

def build_months_kb(year: int, months: list[int]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    names = ["01","02","03","04","05","06","07","08","09","10","11","12"]
    if not months:
        kb.button(text="(Нет данных)", callback_data="patients:nodata")
    else:
        for m in months:
            kb.button(text=names[m-1], callback_data=f"patients:ym:{year}:{m:02d}")
        kb.adjust(6)
    kb.button(text="⬅️ Назад к годам", callback_data="patients:pick_year")
    return kb

@router.callback_query(F.data == "patients:pick_year")
async def cb_pick_year(cb: CallbackQuery):
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    if doctor_id is None:
        await cb.message.edit_text("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        await cb.answer()
        return
    years = list_years_with_referrals(doctor_id)
    await cb.message.edit_text("Выберите год:", reply_markup=build_years_kb(years).as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("patients:year:"))
async def cb_pick_month(cb: CallbackQuery):
    parts = cb.data.split(":")
    year = int(parts[2])
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    if doctor_id is None:
        await cb.message.edit_text("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        await cb.answer()
        return
    months = list_months_for_year(doctor_id, year)
    await cb.message.edit_text(
        f"Выбран год {year}. Выберите месяц:",
        reply_markup=build_months_kb(year, months).as_markup()
    )
    await cb.answer()

@router.callback_query(F.data.startswith("patients:ym:"))
async def cb_list_year_month(cb: CallbackQuery):
    _, _, y, m = cb.data.split(":")
    year = int(y); month = int(m)
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    if doctor_id is None:
        await cb.message.edit_text("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        await cb.answer()
        return
    items = list_referrals_by_doctor(doctor_id, year=year, month=month)
    await _send_list_and_quick(cb.message, items, f"Пациенты за {month:02d}.{year}")
    await cb.answer()

# ---- Рассчитанные / Не рассчитанные ----
@router.callback_query(F.data == "patients:settled")
async def cb_patients_settled(cb: CallbackQuery):
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    if doctor_id is None:
        await cb.message.edit_text("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        await cb.answer()
        return
    items = list_referrals_by_doctor(doctor_id)
    await send_referral_list_numbered(
        cb.message, items, "Рассчитанные пациенты", show_settled_info=True, filter_settled=1
    )
    await cb.answer()

@router.callback_query(F.data == "patients:unsettled")
async def cb_patients_unsettled(cb: CallbackQuery):
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    if doctor_id is None:
        await cb.message.edit_text("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        await cb.answer()
        return
    items = list_referrals_by_doctor(doctor_id)
    await send_referral_list_numbered(
        cb.message, items, "Не рассчитанные пациенты", show_settled_info=False, filter_settled=0
    )
    await cb.answer()

# =========================
# Удаление направлений
# =========================
@router.callback_query(F.data == "qa:delete_menu")
async def qa_delete_menu(cb: CallbackQuery):
    await cb.message.answer("Выберите, откуда удалять:", reply_markup=build_delete_menu().as_markup())
    await cb.answer()

async def _start_delete_flow(message: Message, items: list[dict], title: str, state: FSMContext):
    if not items:
        await message.answer(f"{title}\nСписок пуст.")
        await send_quick_actions(message)
        return

    await send_referral_list_numbered(message, items, title)

    num2id = [int(r["referral_id"]) for r in items]
    await state.update_data(del_num2id=num2id, del_title=title)

    await message.answer(
        "Введите номера для удаления (например: `3, 5-7, 12`).\n"
        "Для отмены — напишите `отмена`.",
        parse_mode="Markdown"
    )
    await state.set_state(DeleteReferral.waiting_numbers)

@router.callback_query(F.data == "del:all")
async def del_all(cb: CallbackQuery, state: FSMContext):
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    items = list_referrals_by_doctor(doctor_id) if doctor_id else []
    await _start_delete_flow(cb.message, items, "Удаление: весь список", state)
    await cb.answer()

@router.callback_query(F.data == "del:cur_month")
async def del_cur_month(cb: CallbackQuery, state: FSMContext):
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    today = date.today()
    items = list_referrals_by_doctor(doctor_id, year=today.year, month=today.month) if doctor_id else []
    await _start_delete_flow(cb.message, items, f"Удаление: {today.month:02d}.{today.year}", state)
    await cb.answer()

@router.callback_query(F.data == "del:pick_year")
async def del_pick_year(cb: CallbackQuery):
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    if not doctor_id:
        await cb.message.answer("Сначала зарегистрируйтесь: отправьте ваши ФИО одной строкой.")
        await cb.answer()
        return
    years = list_years_with_referrals(doctor_id)
    kb = InlineKeyboardBuilder()
    if years:
        for y in years:
            kb.button(text=str(y), callback_data=f"del:year:{y}")
        kb.adjust(4)
    else:
        kb.button(text="(Нет данных)", callback_data="patients:nodata")
    kb.button(text="⬅️ Назад", callback_data="qa:delete_menu")
    await cb.message.answer("Выберите год:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("del:year:"))
async def del_pick_month(cb: CallbackQuery):
    _, _, y = cb.data.split(":")
    year = int(y)
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    months = list_months_for_year(doctor_id, year) if doctor_id else []
    kb = InlineKeyboardBuilder()
    names = ["01","02","03","04","05","06","07","08","09","10","11","12"]
    if months:
        for m in months:
            kb.button(text=names[m-1], callback_data=f"del:ym:{year}:{m:02d}")
        kb.adjust(6)
    else:
        kb.button(text="(Нет данных)", callback_data="patients:nodata")
    kb.button(text="⬅️ Назад к годам", callback_data="del:pick_year")
    await cb.message.answer(f"Выбран год {year}. Выберите месяц:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("del:ym:"))
async def del_list_year_month(cb: CallbackQuery, state: FSMContext):
    _, _, y, m = cb.data.split(":")
    year, month = int(y), int(m)
    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    items = list_referrals_by_doctor(doctor_id, year=year, month=month) if doctor_id else []
    await _start_delete_flow(cb.message, items, f"Удаление: {month:02d}.{year}", state)
    await cb.answer()

def _expand_numbers(spec: str, max_n: int) -> list[int]:
    """
    '3, 5-7, 12' -> [3,5,6,7,12]
    """
    result: set[int] = set()
    tokens = [t.strip() for t in spec.replace(";", ",").split(",") if t.strip()]
    for t in tokens:
        if "-" in t:
            a, b = t.split("-", 1)
            if not a.isdigit() or not b.isdigit():
                continue
            x, y = int(a), int(b)
            if x > y:
                x, y = y, x
            for k in range(x, y + 1):
                if 1 <= k <= max_n:
                    result.add(k)
        else:
            if t.isdigit():
                k = int(t)
                if 1 <= k <= max_n:
                    result.add(k)
    return sorted(result)

@router.message(DeleteReferral.waiting_numbers)
async def del_waiting_numbers(msg: Message, state: FSMContext):
    text = (msg.text or "").strip().lower()
    if text in {"отмена", "cancel", "stop"}:
        await state.clear()
        await msg.answer("Отменено.")
        await send_quick_actions(msg)
        return

    data = await state.get_data()
    num2id: list[int] = data.get("del_num2id") or []
    if not num2id:
        await state.clear()
        await msg.answer("Истёк контекст удаления. Попробуйте снова через меню удаления.")
        await send_quick_actions(msg)
        return

    nums = _expand_numbers(text, len(num2id))
    if not nums:
        await msg.answer("Не распознал номера. Введите, например: `3, 5-7, 12` или `отмена`.", parse_mode="Markdown")
        return

    sel_ids = [num2id[i - 1] for i in nums]
    await state.update_data(del_selected_ids=sel_ids, del_selected_nums=nums)

    preview = ", ".join(map(str, nums))
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Удалить", callback_data="del:confirm")
    kb.button(text="❌ Отмена", callback_data="del:cancel")
    kb.adjust(2)

    await msg.answer(
        f"Подтвердите удаление: номеров [{preview}] (всего {len(nums)}).",
        reply_markup=kb.as_markup()
    )

@router.callback_query(F.data == "del:confirm")
async def del_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel_ids: list[int] = data.get("del_selected_ids") or []

    tg_user_id = cb.from_user.id
    doctor_id = get_doctor_id_by_tg(tg_user_id)
    deleted = delete_referrals(doctor_id, sel_ids) if (doctor_id and sel_ids) else 0

    await state.clear()
    await cb.message.answer(f"Удалено направлений: {deleted}.")
    await send_quick_actions(cb.message)
    await cb.answer()

@router.callback_query(F.data == "del:cancel")
async def del_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Отменено.")
    await send_quick_actions(cb.message)
    await cb.answer()

# ---- Быстрые действия (прочее) ----
@router.callback_query(F.data == "qa:add_patient")
async def qa_add_patient(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите ФИО пациента (одной строкой).")
    await state.set_state(AddPatient.waiting_patient_full_name)
    await cb.answer()

@router.callback_query(F.data == "qa:patients_menu")
async def qa_patients_menu(cb: CallbackQuery):
    await cb.message.answer("Выберите режим:", reply_markup=build_patients_menu().as_markup())
    await cb.answer()

@router.callback_query(F.data == "qa:help")
async def qa_help(cb: CallbackQuery):
    await cb.message.answer(fmt_help(initialized=True))
    await send_quick_actions(cb.message)
    await cb.answer()

# =========================
# Entrypoint
# =========================
async def main():
    migrate()
    bot = Bot(get_doctor_token())

    from aiogram.enums import ParseMode

    bot = Bot(get_doctor_token(), parse_mode=ParseMode.HTML)

    # для самопроверки токена — видно в логе, каким ботом запущено
    me = await bot.get_me()
    log.info(f"Running as @{me.username} (id={me.id})")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    log.info("Doctor bot starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("Fatal error in doctor bot")
        raise
