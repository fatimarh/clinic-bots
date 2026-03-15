# bots/admin_bot.py
import asyncio
from collections import defaultdict
from pathlib import Path

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder

from common.config import get_admin_token, get_admin_access_pass, get_doctor_token
from common.logging_config import setup_logger
from common.db import (
    migrate,
    admin_is_authorized,
    admin_authorize,
    list_doctors_with_counts,
    search_doctors_prefix,
    delete_doctor,
    list_unsettled_referrals_all,
    list_unsettled_referrals_by_doctor,
    settle_referrals,
    get_referrals_details,
    list_settled_current_month,
)

ROOT = Path(__file__).resolve().parents[1]
log = setup_logger("admin_bot", ROOT / "logs" / "admin.log")

router = Router()

# второй бот для уведомлений врачей
doctor_notify_bot: Bot | None = None

# ------------------ utils ------------------

def _fmt_help() -> str:
    return (
        "Админ-бот. Доступные действия:\n"
        "• 👨‍⚕️ Врачи — список врачей и их направлений\n"
        "• 🔎 Поиск врача — быстрый поиск по ФИО\n"
        "• 🗑 Удалить врача — по номеру из последнего списка\n"
        "• 🆕 Новые (не рассчитанные) — отметить «рассчитано» по номерам\n"
        "• ✅ Рассчитанные — за текущий месяц\n"
        "• 📤 Экспорт — скоро\n"
    )

def _kb_main() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="👨‍⚕️ Врачи", callback_data="adm:doctors")
    kb.button(text="🔎 Поиск врача", callback_data="adm:find")
    kb.button(text="🗑 Удалить врача", callback_data="adm:del_doctor")
    kb.button(text="🆕 Новые (не рассчитанные)", callback_data="adm:new_unsettled")
    kb.button(text="✅ Рассчитанные", callback_data="adm:settled_current")
    kb.button(text="📤 Экспорт (скоро)", callback_data="adm:export_soon")
    kb.adjust(1)
    return kb

async def _send_chunked(msg: Message, title: str, lines: list[str]):
    """Отправка длинного списка частями (< 4096 символов)."""
    if not lines:
        await msg.answer(f"{title}\n(пусто)")
        return
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await msg.answer(f"{title}\n{chunk.strip()}")
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await msg.answer(f"{title}\n{chunk.strip()}")

def _fmt_ru_date(iso_ts: str | None) -> str:
    if not iso_ts:
        return "???.???.????"
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(iso_ts).strftime("%d.%m.%Y")
    except Exception:
        return iso_ts

def _expand_numbers(spec: str, max_n: int) -> list[int]:
    """
    '3, 5-7, 12' -> [3,5,6,7,12]
    """
    result: set[int] = set()
    tokens = [t.strip() for t in (spec or "").replace(";", ",").split(",") if t.strip()]
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

# ------------------ FSM ------------------

class DelDoctor(StatesGroup):
    waiting_number = State()
    confirm = State()

class FindDoctor(StatesGroup):
    waiting_query = State()

class MarkSettled(StatesGroup):
    waiting_numbers = State()
    confirm = State()

# ------------------ Handlers ------------------

@router.message(CommandStart())
async def start_handler(msg: Message):
    if admin_is_authorized(msg.chat.id):
        await msg.answer("Вы уже авторизованы.\n" + _fmt_help(), reply_markup=_kb_main().as_markup())
    else:
        await msg.answer("Отправьте секретный пароль для доступа.")

@router.message(Command("help"))
async def help_handler(msg: Message):
    if not admin_is_authorized(msg.chat.id):
        await msg.answer("Сначала авторизуйтесь — отправьте секретный пароль.")
        return
    await msg.answer(_fmt_help(), reply_markup=_kb_main().as_markup())

# ВАЖНО: ловим только сообщения ВНЕ FSM (чтобы не перехватывать ввод в сценах поиска/удаления)
@router.message(StateFilter(None))
async def auth_or_ignore(msg: Message):
    if admin_is_authorized(msg.chat.id):
        return
    secret = get_admin_access_pass()
    if msg.text and msg.text.strip() == secret:
        admin_authorize(msg.chat.id)
        await msg.answer("✅ Авторизация успешна.", reply_markup=_kb_main().as_markup())
        log.info(f"Admin chat authorized: {msg.chat.id}")
    else:
        # молча игнорируем
        pass

# ---- main menu actions ----

@router.callback_query(F.data == "adm:export_soon")
async def export_soon(cb: CallbackQuery):
    await cb.message.answer("📤 Экспорт в Excel будет добавлен на следующем шаге.")
    await cb.message.answer(_fmt_help(), reply_markup=_kb_main().as_markup())
    await cb.answer()

@router.callback_query(F.data == "adm:doctors")
async def list_doctors(cb: CallbackQuery, state: FSMContext):
    data = list_doctors_with_counts()
    if not data:
        await cb.message.answer("Список врачей пуст.")
        await cb.message.answer(_fmt_help(), reply_markup=_kb_main().as_markup())
        await cb.answer()
        return

    lines = [f"{i}. {d['full_name']} — направлений: {d['referrals_count']}"
             for i, d in enumerate(data, 1)]
    await cb.message.answer("👨‍⚕️ Список врачей:")
    await _send_chunked(cb.message, "Врачи:", lines)   # <-- await!

    # map: index -> doctor_id, и сами строки списка
    num2id = [int(d["doctor_id"]) for d in data]
    await state.update_data(last_doctor_list=num2id, last_doctor_lines=lines)

    await cb.message.answer("Выберите действие:", reply_markup=_kb_main().as_markup())
    await cb.answer()

@router.callback_query(F.data == "adm:find")
async def find_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите начало ФИО для поиска (например, «Ива»).")
    await state.set_state(FindDoctor.waiting_query)
    await cb.answer()

@router.message(FindDoctor.waiting_query)
async def find_apply(msg: Message, state: FSMContext):
    q = (msg.text or "").strip()
    res = search_doctors_prefix(q)
    if not res:
        await msg.answer("Ничего не найдено.")
        await msg.answer("Выберите действие:", reply_markup=_kb_main().as_markup())
        await state.clear()
        return

    lines = [f"{i}. {d['full_name']} — направлений: {d['referrals_count']}" for i, d in enumerate(res, 1)]
    await _send_chunked(msg, f"Результат поиска «{q}»: ", lines)  # <-- await!
    await state.update_data(last_doctor_list=[int(d["doctor_id"]) for d in res],
                            last_doctor_lines=lines)
    await msg.answer("Выберите действие:", reply_markup=_kb_main().as_markup())
    await state.clear()

@router.callback_query(F.data == "adm:del_doctor")
async def del_doctor_start(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    last_ids = data.get("last_doctor_list")
    last_lines = data.get("last_doctor_lines")
    if not last_ids:
        await cb.message.answer("Сначала откройте список врачей (кнопка «Врачи»), затем запускайте удаление.")
        await cb.answer()
        return

    # Переотправим последний список для удобства выбора
    await cb.message.answer("👨‍⚕️ Список врачей (последний вывод):")
    await _send_chunked(cb.message, "Врачи:", last_lines or [])
    await cb.message.answer("Введите номер врача из последнего списка:")
    await state.set_state(DelDoctor.waiting_number)
    await cb.answer()

@router.message(DelDoctor.waiting_number)
async def del_doctor_number(msg: Message, state: FSMContext):
    data = await state.get_data()
    last: list[int] = data.get("last_doctor_list") or []
    text = (msg.text or "").strip()
    if not text.isdigit():
        await msg.answer("Ожидался номер. Попробуйте ещё раз или нажмите /help")
        return
    idx = int(text)
    if idx < 1 or idx > len(last):
        await msg.answer("Неверный номер из последнего списка.")
        return

    doctor_id = last[idx - 1]
    # найдём имя для подтверждения
    name = "врач"
    for i, d in enumerate(list_doctors_with_counts(), 1):
        if d["doctor_id"] == doctor_id:
            name = d["full_name"]
            break

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Удалить", callback_data=f"adm:del_confirm:{doctor_id}")
    kb.button(text="❌ Отмена", callback_data="adm:del_cancel")
    kb.adjust(2)
    await msg.answer(f"Удалить №{idx}: {name} ?", reply_markup=kb.as_markup())
    await state.set_state(DelDoctor.confirm)

@router.callback_query(F.data.startswith("adm:del_confirm:"))
async def del_doctor_confirm(cb: CallbackQuery, state: FSMContext):
    doctor_id = int(cb.data.split(":")[-1])
    deleted = delete_doctor(doctor_id)
    await cb.message.answer(f"Удалено врачей: {deleted}. (Списки будут сдвинуты при следующем выводе)")
    # cбросим кэш списка, чтобы вынудить заново открыть «Врачи»
    await state.update_data(last_doctor_list=None, last_doctor_lines=None)
    await cb.message.answer("Выберите действие:", reply_markup=_kb_main().as_markup())
    await cb.answer()

@router.callback_query(F.data == "adm:del_cancel")
async def del_doctor_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Отменено.")
    await cb.message.answer("Выберите действие:", reply_markup=_kb_main().as_markup())
    await cb.answer()

# ---- Новые направления (не рассчитанные) ----

@router.callback_query(F.data == "adm:new_unsettled")
async def new_unsettled_menu(cb: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="Все не рассчитанные", callback_data="adm:uns_all")
    kb.button(text="По врачу", callback_data="adm:uns_choose_doctor")
    kb.button(text="⬅️ Назад", callback_data="adm:back")
    kb.adjust(1)
    await cb.message.answer("Выберите:", reply_markup=kb.as_markup())
    await cb.answer()

def _lines_for_unsettled(items: list[dict]) -> list[str]:
    lines = []
    for i, r in enumerate(items, 1):
        fio = r["patient_full_name"]
        bd = r.get("patient_birth_date") or ""
        if bd:
            try:
                from datetime import datetime as _dt
                bd = _dt.strptime(bd, "%Y-%m-%d").strftime("%d.%m.%Y")
            except Exception:
                pass
        dadd = _fmt_ru_date(r.get("created_at"))
        doc = r["doctor_full_name"]
        lines.append(f"{i}. {fio} {bd} — направил: {doc} — дата: {dadd}")
    return lines

async def _start_mark_flow(msg: Message, items: list[dict], state: FSMContext, title: str):
    if not items:
        await msg.answer(f"{title}\nСписок пуст.")
        await msg.answer(_fmt_help(), reply_markup=_kb_main().as_markup())
        return

    await _send_chunked(msg, title, _lines_for_unsettled(items))  # <-- await!

    # сохраним соответствие
    num2id = [int(r["referral_id"]) for r in items]
    await state.update_data(mark_num2id=num2id)

    await msg.answer(
        "Введите номера для отметки «рассчитано» (например: `3, 5-7, 12`).\n"
        "Для отмены — напишите `отмена`.",
        parse_mode="Markdown"
    )
    await state.set_state(MarkSettled.waiting_numbers)

@router.callback_query(F.data == "adm:uns_all")
async def uns_all(cb: CallbackQuery, state: FSMContext):
    items = list_unsettled_referrals_all()
    await _start_mark_flow(cb.message, items, state, "Новые направления (все не рассчитанные)")
    await cb.answer()

@router.callback_query(F.data == "adm:uns_choose_doctor")
async def uns_choose_doctor(cb: CallbackQuery, state: FSMContext):
    docs = list_doctors_with_counts()
    if not docs:
        await cb.message.answer("Список врачей пуст.")
        await cb.answer()
        return
    kb = InlineKeyboardBuilder()
    for i, d in enumerate(docs, 1):
        kb.button(text=f"{i}. {d['full_name']}", callback_data=f"adm:uns_doc:{d['doctor_id']}")
    kb.adjust(1)
    await cb.message.answer("Выберите врача:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("adm:uns_doc:"))
async def uns_for_doctor(cb: CallbackQuery, state: FSMContext):
    doctor_id = int(cb.data.split(":")[-1])
    items = list_unsettled_referrals_by_doctor(doctor_id)
    await _start_mark_flow(cb.message, items, state, "Новые направления (по врачу)")
    await cb.answer()

@router.message(MarkSettled.waiting_numbers)
async def mark_numbers(msg: Message, state: FSMContext):
    text = (msg.text or "").strip().lower()
    if text in {"отмена", "cancel", "stop"}:
        await state.clear()
        await msg.answer("Отменено.")
        await msg.answer(_fmt_help(), reply_markup=_kb_main().as_markup())
        return

    data = await state.get_data()
    num2id: list[int] = data.get("mark_num2id") or []
    if not num2id:
        await state.clear()
        await msg.answer("Истёк контекст. Откройте список заново.")
        await msg.answer(_fmt_help(), reply_markup=_kb_main().as_markup())
        return

    nums = _expand_numbers(text, len(num2id))
    if not nums:
        await msg.answer("Не распознал номера. Введите, например: `3, 5-7, 12` или `отмена`.", parse_mode="Markdown")
        return

    ref_ids = [num2id[i - 1] for i in nums]
    await state.update_data(mark_selected_ids=ref_ids, mark_selected_nums=nums)

    preview = ", ".join(map(str, nums))
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отметить рассчитано", callback_data="adm:mark_confirm")
    kb.button(text="❌ Отмена", callback_data="adm:mark_cancel")
    kb.adjust(2)

    await msg.answer(f"Подтвердите отметку «рассчитано» для номеров [{preview}] (всего {len(nums)}).",
                     reply_markup=kb.as_markup())

@router.callback_query(F.data == "adm:mark_confirm")
async def mark_confirm(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ref_ids: list[int] = data.get("mark_selected_ids") or []

    updated = settle_referrals(ref_ids)
    await state.clear()

    # соберём уведомления врачам (группами)
    details = get_referrals_details(ref_ids)
    per_doctor: dict[int, list[dict]] = defaultdict(list)
    for r in details:
        per_doctor[int(r["doctor_tg_user_id"])].append(r)

    # отправим каждому врачу одно сообщение (если есть tg_user_id)
    global doctor_notify_bot
    if doctor_notify_bot is not None:
        for tg_uid, rows in per_doctor.items():
            if not tg_uid:
                continue
            lines = []
            for r in rows:
                fio = r["patient_full_name"]
                bd = r.get("patient_birth_date") or ""
                if bd:
                    try:
                        from datetime import datetime as _dt
                        bd = _dt.strptime(bd, "%Y-%m-%d").strftime("%d.%m.%Y")
                    except Exception:
                        pass
                dadd = _fmt_ru_date(r.get("created_at"))
                lines.append(f"• {fio} {bd} — дата направления {dadd}")
            text = "✅ Рассчитались за:\n" + "\n".join(lines)
            try:
                await doctor_notify_bot.send_message(chat_id=tg_uid, text=text)
            except Exception as e:
                log.warning(f"Notify doctor {tg_uid} failed: {e}")

    await cb.message.answer(f"Отмечено «рассчитано»: {updated}.")
    await cb.message.answer(_fmt_help(), reply_markup=_kb_main().as_markup())
    await cb.answer()

@router.callback_query(F.data == "adm:mark_cancel")
async def mark_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Отменено.")
    await cb.message.answer(_fmt_help(), reply_markup=_kb_main().as_markup())
    await cb.answer()

# ---- Рассчитанные (за текущий месяц) ----

@router.callback_query(F.data == "adm:settled_current")
async def settled_current(cb: CallbackQuery):
    rows = list_settled_current_month()
    if not rows:
        await cb.message.answer("За текущий месяц рассчитанных нет.")
        await cb.message.answer(_fmt_help(), reply_markup=_kb_main().as_markup())
        await cb.answer()
        return

    lines = []
    for i, r in enumerate(rows, 1):
        fio = r["patient_full_name"]
        bd = r.get("patient_birth_date") or ""
        if bd:
            try:
                from datetime import datetime as _dt
                bd = _dt.strptime(bd, "%Y-%m-%d").strftime("%d.%m.%Y")
            except Exception:
                pass
        dadd = _fmt_ru_date(r.get("created_at"))
        dset = _fmt_ru_date(r.get("settled_at"))
        doc = r["doctor_full_name"]
        lines.append(f"{i}. {fio} {bd} — {doc} — направление: {dadd} — рассчитан: {dset}")

    await _send_chunked(cb.message, "Рассчитанные за текущий месяц:", lines)  # <-- await!
    await cb.message.answer(_fmt_help(), reply_markup=_kb_main().as_markup())
    await cb.answer()

@router.callback_query(F.data == "adm:back")
async def go_back(cb: CallbackQuery):
    await cb.message.answer(_fmt_help(), reply_markup=_kb_main().as_markup())
    await cb.answer()

# ------------------ Entrypoint ------------------

async def main():
    migrate()
    admin_bot = Bot(get_admin_token())

    # второй бот для уведомлений врачей
    global doctor_notify_bot
    doctor_notify_bot = Bot(get_doctor_token())

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    log.info("Admin bot starting...")
    await dp.start_polling(admin_bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("Fatal error in admin bot")
        raise