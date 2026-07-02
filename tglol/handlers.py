from __future__ import annotations

from html import escape
from math import ceil
from pathlib import Path
import re
import secrets
import shutil

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message, TelegramObject
from telethon.errors import SessionPasswordNeededError

from tglol.config import Config
from tglol.db import (
    add_department,
    add_proxy,
    add_worker,
    assign_account_to_worker,
    assign_proxy_to_worker,
    count_accounts,
    count_accounts_by_stage,
    delete_department,
    delete_worker,
    get_account,
    get_department,
    get_worker,
    get_worker_by_telegram_id,
    list_accounts,
    list_departments,
    list_proxies,
    list_workers,
    proxy_exists,
    set_account_stage,
    worker_exists,
)
from tglol.desktop_profile import generated_account_json, random_desktop_runtime, utc_now_iso
from tglol.importer import download_document, import_session_account, import_zip
from tglol.json_utils import load_json, pick_api, runtime_from_json, write_json
from tglol.keyboards import (
    ACCOUNTS_PER_PAGE,
    account_detail_menu,
    accounts_menu,
    accounts_page_keyboard,
    add_account_menu,
    add_account_target_menu,
    assign_account_keyboard,
    confirm_account_stage_menu,
    confirm_worker_account_stage_menu,
    confirm_delete_department_menu,
    confirm_delete_worker_menu,
    department_detail_menu,
    departments_menu,
    digit_code_keyboard,
    main_menu,
    placeholder_menu,
    proxies_menu,
    skip_json_menu,
    worker_department_select_menu,
    worker_account_sections_menu,
    worker_detail_menu,
    worker_self_account_detail_menu,
    worker_self_accounts_page_keyboard,
    worker_self_menu,
    worker_storage_keyboard,
    workers_menu,
)
from tglol.paths import unique_path
from tglol.states import AddByCode, AddBySession, AddByZip, AddProxy, AssignProxy, CreateDepartment, CreateWorker
from tglol.telegram_service import get_latest_telegram_code, send_code, sign_in_code, sign_in_password, user_fields

router = Router()


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        config: Config = data["config"]
        user = data.get("event_from_user")
        if not user:
            return None

        if user.id in config.admin_ids:
            data["is_admin"] = True
            data["current_worker"] = None
            return await handler(event, data)

        worker = get_worker_by_telegram_id(config, user.id)
        if worker:
            data["is_admin"] = False
            data["current_worker"] = worker
            if isinstance(event, Message):
                text = event.text or ""
                if text.startswith("/start") or text == "/cancel":
                    return await handler(event, data)
            elif isinstance(event, CallbackQuery):
                callback_data = event.data or ""
                allowed = (
                    callback_data == "noop"
                    or callback_data == "worker:self:menu"
                    or callback_data.startswith("worker:self:page:")
                    or callback_data.startswith("worker:self_account:")
                    or callback_data.startswith("worker:self_code:")
                    or callback_data.startswith("worker:self_phone:")
                    or callback_data.startswith("worker:self_stage_")
                )
                if allowed:
                    return await handler(event, data)
                await event.answer("Нет доступа к этому разделу.", show_alert=True)
                return None

            if isinstance(event, Message):
                await event.answer("Нет доступа.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Нет доступа.", show_alert=True)
            return None

        if isinstance(event, Message):
            await event.answer("Нет доступа.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Нет доступа.", show_alert=True)
        return None


router.message.outer_middleware(AccessMiddleware())
router.callback_query.outer_middleware(AccessMiddleware())


def _pages(total: int) -> int:
    return max(1, ceil(total / ACCOUNTS_PER_PAGE))


def _account_name(account) -> str:
    return str(account.phone or account.username or account.telegram_user_id or "без данных")


def _text(value) -> str:
    return escape(str(value)) if value not in (None, "") else "-"


def _copyable(value) -> str:
    return f"<code>{escape(str(value))}</code>" if value not in (None, "") else "-"


def _username(value) -> str:
    if value in (None, ""):
        return "-"
    username = str(value)
    if not username.startswith("@"):
        username = f"@{username}"
    return f"<code>{escape(username)}</code>"


def _worker_name(worker) -> str:
    if not worker:
        return "-"
    return worker["name"]


def _worker_scope(worker) -> tuple[int | None | str, int | None | str]:
    if worker["department_id"]:
        return "any", worker["department_id"]
    return worker["id"], "any"


def _worker_account_counts(config: Config, worker) -> tuple[int, int, int]:
    worker_id, department_id = _worker_scope(worker)
    total = count_accounts_by_stage(
        config,
        worker_id=worker_id,
        department_id=department_id,
    )
    nereg_count = count_accounts_by_stage(
        config,
        worker_id=worker_id,
        department_id=department_id,
        account_stage="nereg",
    )
    reg_count = count_accounts_by_stage(
        config,
        worker_id=worker_id,
        department_id=department_id,
        account_stage="reg",
    )
    return total, nereg_count, reg_count


def _worker_can_access_account(account, worker) -> bool:
    if worker["department_id"]:
        return account.department_id == worker["department_id"]
    return account.worker_id == worker["id"]


def _worker_department_title(config: Config, worker) -> str:
    department = get_department(config, worker["department_id"]) if worker["department_id"] else None
    return department["name"] if department else "Без отдела"


def _worker_stage_title(stage: str) -> str:
    return "РЕГ" if stage == "reg" else "НЕРЕГ"


def _normalize_login_code(raw: str) -> str:
    return "".join(ch for ch in raw if ch.isdigit())


def _normalize_login_phone(raw: str) -> str | None:
    raw = (raw or "").strip()
    digits = re.sub(r"\D+", "", raw)
    if raw.startswith("00") and len(digits) > 2:
        digits = digits[2:]
    if not 8 <= len(digits) <= 15:
        return None
    return f"+{digits}"


def _delivery_type_label(raw_type: str | None) -> str:
    labels = {
        "Authorized": "сессия уже авторизована",
        "SentCodeTypeApp": "в Telegram-приложение аккаунта",
        "SentCodeTypeSms": "SMS",
        "SentCodeTypeCall": "звонком",
        "SentCodeTypeFlashCall": "flash-call",
        "SentCodeTypeMissedCall": "пропущенным звонком",
        "SentCodeTypeEmailCode": "на email",
        "CodeTypeSms": "SMS",
        "CodeTypeCall": "звонком",
        "CodeTypeFlashCall": "flash-call",
        "CodeTypeMissedCall": "пропущенным звонком",
        "CodeTypeFragmentSms": "Fragment SMS",
    }
    return labels.get(raw_type or "", raw_type or "неизвестно")


def _code_request_text(request) -> str:
    lines = [
        "Telegram принял запрос кода.",
        f"Куда отправлен: {_delivery_type_label(request.delivery_type)}",
        f"Raw type: {request.delivery_type}",
    ]
    if request.code_length:
        lines.append(f"Длина кода: {request.code_length} цифр")
    if request.next_type:
        lines.append(f"Следующий способ: {_delivery_type_label(request.next_type)}")
    if request.timeout:
        lines.append(f"Повторный запрос будет доступен примерно через {request.timeout} сек.")
        lines.append("Не жми повторный запрос без необходимости: Telegram может сменить доставку на звонок/SMS.")
    if request.delivery_type == "SentCodeTypeApp":
        lines.append("")
        lines.append("Важно: это НЕ SMS. Код должен прийти в уже активную сессию этого аккаунта.")
        lines.append("Если активной сессии нет под рукой, бот не сможет сам вытащить этот первый код: Telegram не отдает его через API до входа.")
    elif request.delivery_type in {"SentCodeTypeSms", "CodeTypeSms", "CodeTypeFragmentSms"}:
        lines.append("")
        lines.append("Это SMS/Fragment-доставка. Код нужно смотреть не в Telegram-чате, а в SMS/Fragment.")
    elif request.delivery_type in {"SentCodeTypeCall", "SentCodeTypeMissedCall", "CodeTypeCall", "CodeTypeMissedCall"}:
        lines.append("")
        lines.append("Telegram сам выбрал доставку звонком. Это бывает и у уже существующих аккаунтов.")
        lines.append("Код нужно брать из звонка/последних цифр номера.")
    lines.append("")
    lines.append("Введите код кнопками или одним сообщением.")
    return "\n".join(lines)


def _code_entry_text(info_text: str, code: str) -> str:
    current = code if code else "-"
    return f"{info_text}\n\nТекущий ввод: <code>{current}</code>"


def _promote_login_session(config: Config, temp_session_path: Path, phone: str, login_id: str) -> Path:
    phone_digits = phone.lstrip("+")
    final_path = unique_path(config.sessions_dir, f"{phone_digits}_{login_id}.session")
    if temp_session_path.resolve() == final_path.resolve():
        return final_path
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if not temp_session_path.exists():
        raise RuntimeError(f"Temporary session file not found: {temp_session_path}")
    shutil.move(str(temp_session_path), str(final_path))
    return final_path


def _worker_counts(config: Config, worker) -> tuple[int, int]:
    _, nereg_count, reg_count = _worker_account_counts(config, worker)
    return nereg_count, reg_count


async def _show_worker_home_callback(callback: CallbackQuery, config: Config, worker) -> None:
    nereg_count, reg_count = _worker_counts(config, worker)
    text = (
        f"Рабочая панель\n"
        f"Отдел: {_worker_department_title(config, worker)}\n\n"
        f"НЕРЕГ: {nereg_count}\n"
        f"РЕГ: {reg_count}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=worker_self_menu(
            department_name=_worker_department_title(config, worker),
            nereg_count=nereg_count,
            reg_count=reg_count,
        ),
    )
    await callback.answer()


async def _show_worker_home_message(message: Message, config: Config, worker) -> None:
    nereg_count, reg_count = _worker_counts(config, worker)
    text = (
        f"Рабочая панель\n"
        f"Отдел: {_worker_department_title(config, worker)}\n\n"
        f"НЕРЕГ: {nereg_count}\n"
        f"РЕГ: {reg_count}"
    )
    await message.answer(
        text,
        reply_markup=worker_self_menu(
            department_name=_worker_department_title(config, worker),
            nereg_count=nereg_count,
            reg_count=reg_count,
        ),
    )


async def _show_account_page(callback: CallbackQuery, config: Config, origin: str, ref_id: int, page: int) -> None:
    worker = None
    worker_filter: int | None | str = None if origin == "common" else ref_id
    department_filter: int | None | str = None if origin == "common" else "any"
    account_stage = None
    if origin == "worker_nereg":
        account_stage = "nereg"
    elif origin == "worker_reg":
        account_stage = "reg"

    if origin in {"worker", "worker_nereg", "worker_reg"}:
        worker = get_worker(config, ref_id)
        if not worker:
            await callback.answer("Воркер не найден.", show_alert=True)
            return
        worker_filter, department_filter = _worker_scope(worker)

    total = count_accounts_by_stage(
        config,
        worker_id=worker_filter,
        department_id=department_filter,
        account_stage=account_stage,
    )
    page = max(0, min(page, _pages(total) - 1))
    accounts = list_accounts(
        config,
        limit=ACCOUNTS_PER_PAGE,
        offset=page * ACCOUNTS_PER_PAGE,
        worker_id=worker_filter,
        department_id=department_filter,
        account_stage=account_stage,
    )

    if origin in {"worker", "worker_nereg", "worker_reg"}:
        section = ""
        if origin == "worker_nereg":
            section = "\nРаздел: НЕРЕГ"
        elif origin == "worker_reg":
            section = "\nРаздел: РЕГ"
        title = f"Хранилище воркера\n{_worker_name(worker)}{section}"
    else:
        title = "Общее хранилище"

    if total == 0:
        text = f"{title}\n\nАккаунтов пока нет."
    else:
        text = f"{title}\n\nВсего: {total}\nСтраница: {page + 1}/{_pages(total)}"

    await callback.message.edit_text(
        text,
        reply_markup=accounts_page_keyboard(
            accounts,
            total=total,
            page=page,
            origin=origin,
            ref_id=ref_id,
        ),
    )
    await callback.answer()


def _account_detail_text(account, config: Config) -> str:
    worker = get_worker(config, account.worker_id) if account.worker_id else None
    department = get_department(config, account.department_id) if account.department_id else None
    stage = "РЕГ" if account.account_stage == "reg" else "НЕРЕГ"
    return (
        f"<b>Аккаунт #{account.id}</b> · {stage}\n"
        f"Статус: <code>{_text(account.status)}</code>\n\n"
        f"Телефон:\n{_copyable(account.phone)}\n\n"
        f"Username: {_username(account.username)}\n"
        f"User ID: {_copyable(account.telegram_user_id)}\n\n"
        f"JSON: {_text(account.json_source)}\n"
        f"Источник: {_text(account.source_type)}\n"
        f"Воркер: {_text(_worker_name(worker))}\n"
        f"Отдел: {_text(department['name'] if department else None)}"
    )


def _worker_account_detail_text(account) -> str:
    stage = "РЕГ" if account.account_stage == "reg" else "НЕРЕГ"
    return (
        f"<b>Аккаунт #{account.id}</b> · {stage}\n"
        f"Статус: <code>{_text(account.status)}</code>\n\n"
        f"Телефон:\n{_copyable(account.phone)}\n\n"
        f"Username: {_username(account.username)}"
    )


def _account_connection_params(account, config: Config) -> tuple[int, str, dict[str, str]]:
    data = None
    raw_path = account.json_original_path or account.json_effective_path
    if raw_path:
        path = Path(raw_path)
        if path.exists():
            data = load_json(path)
    api_id, api_hash = pick_api(data, config)
    runtime = runtime_from_json(data or {})
    return api_id, api_hash, runtime


def _add_target_worker_id(data: dict) -> int | None:
    value = data.get("add_target_worker_id")
    if value in (None, "", 0, "0"):
        return None
    return int(value)


def _add_target_label(config: Config, worker_id: int | None) -> str:
    if worker_id is None:
        return "Основное хранилище"
    worker = get_worker(config, worker_id)
    return worker["name"] if worker else "Воркер не найден"


def _apply_added_account_destination(config: Config, account_id: int, worker_id: int | None) -> None:
    set_account_stage(config, account_id, "nereg")
    if worker_id is not None:
        assign_account_to_worker(config, account_id, worker_id)


def _apply_import_results_destination(config: Config, results, worker_id: int | None) -> None:
    for result in results:
        if result.account_id:
            _apply_added_account_destination(config, result.account_id, worker_id)


async def finalize_code_login(
    message: Message,
    state: FSMContext,
    config: Config,
    *,
    twofa: str | None,
    user,
) -> None:
    data = await state.get_data()
    session_path = _promote_login_session(
        config,
        Path(data["session_path"]),
        data["phone"],
        data["login_id"],
    )
    runtime = data["runtime"]
    fields = user_fields(user)
    json_path = unique_path(config.json_dir, session_path.with_suffix(".json").name)
    generated = generated_account_json(
        config,
        runtime=runtime,
        twofa=twofa,
        session_file=session_path.name,
        phone=fields["phone"],
        user_id=fields["telegram_user_id"],
        username=fields["username"],
        first_name=fields["first_name"],
        last_name=fields["last_name"],
    )
    write_json(json_path, generated)

    from tglol.db import add_account

    now = utc_now_iso()
    account_id = add_account(
        config,
        {
            "phone": fields["phone"],
            "telegram_user_id": fields["telegram_user_id"],
            "username": fields["username"],
            "first_name": fields["first_name"],
            "last_name": fields["last_name"],
            "session_path": str(session_path),
            "json_original_path": None,
            "json_effective_path": str(json_path),
            "json_source": "generated",
            "twofa_password": twofa,
            "source_type": "code",
            "status": "active",
            "created_by": data.get("admin_id") or (message.from_user.id if message.from_user else None),
            "created_at": now,
            "updated_at": now,
        },
    )
    target_worker_id = _add_target_worker_id(data)
    _apply_added_account_destination(config, account_id, target_worker_id)
    await state.clear()
    await message.answer(
        (
            f"Аккаунт добавлен по коду.\n"
            f"ID: {account_id}\n"
            f"Телефон: {fields['phone'] or '-'}\n"
            f"Хранилище: {_add_target_label(config, target_worker_id)}\n"
            f"Раздел: НЕРЕГ"
        ),
        reply_markup=add_account_target_menu(list_workers(config)),
    )


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(CommandStart())
async def start(
    message: Message,
    state: FSMContext,
    config: Config,
    is_admin: bool,
    current_worker,
) -> None:
    await state.clear()
    if is_admin:
        await message.answer("Админ-панель", reply_markup=main_menu())
        return
    await _show_worker_home_message(message, config, current_worker)


@router.callback_query(F.data == "main:menu")
async def show_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Админ-панель", reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "accounts:menu")
async def show_accounts_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Аккаунты", reply_markup=accounts_menu())
    await callback.answer()


@router.callback_query(F.data == "accounts:add")
async def show_add_account_target_menu(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    await state.clear()
    await callback.message.edit_text(
        "Куда добавить аккаунт?\n\nВсе новые аккаунты попадут в раздел НЕРЕГ.",
        reply_markup=add_account_target_menu(list_workers(config)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("accounts:add_target:"))
async def select_add_account_target(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    parts = callback.data.split(":")
    worker_id = None
    if len(parts) == 4 and parts[2] == "worker":
        worker_id = int(parts[3])
        if not worker_exists(config, worker_id):
            await callback.answer("Воркер не найден.", show_alert=True)
            return
    await state.update_data(add_target_worker_id=worker_id)
    await callback.message.edit_text(
        f"Хранилище: {_add_target_label(config, worker_id)}\nРаздел: НЕРЕГ\n\nВыбери способ добавления аккаунта.",
        reply_markup=add_account_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "accounts:add:code")
async def add_by_code_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddByCode.waiting_phone)
    await callback.message.edit_text(
        "Отправь номер телефона.\n\nМожно с плюсом или без него, например:\n+15074486037\n15074486037"
    )
    await callback.answer()


@router.message(AddByCode.waiting_phone)
async def add_by_code_phone(message: Message, state: FSMContext, config: Config) -> None:
    phone = _normalize_login_phone(message.text or "")
    if not phone:
        await message.answer("Номер некорректный. Отправь номер с кодом страны, например: +15074486037")
        return

    runtime = random_desktop_runtime()
    admin_id = message.from_user.id if message.from_user else 0
    login_id = secrets.token_hex(4)
    phone_digits = phone.lstrip("+")
    session_path = unique_path(config.temp_dir, f"temp_session_{admin_id}_{phone_digits}_{login_id}.session")
    try:
        code_request = await send_code(
            session_path,
            phone,
            config.telegram_api_id,
            config.telegram_api_hash,
            runtime,
        )
    except Exception as exc:
        await state.clear()
        await message.answer(
            f"Не удалось отправить код Telegram: {exc}",
            reply_markup=add_account_target_menu(list_workers(config)),
        )
        return

    await state.update_data(
        phone=phone,
        phone_code_hash=code_request.phone_code_hash,
        session_path=str(session_path),
        login_id=login_id,
        admin_id=admin_id,
        runtime=runtime,
        login_started_at=utc_now_iso(),
        code="",
    )
    if code_request.already_authorized and code_request.user:
        await finalize_code_login(message, state, config, twofa=None, user=code_request.user)
        return
    if code_request.already_authorized:
        await state.clear()
        await message.answer(
            "Сессия уже авторизована, но Telegram не вернул данные аккаунта.",
            reply_markup=add_account_target_menu(list_workers(config)),
        )
        return

    info_text = _code_request_text(code_request)
    await state.update_data(code_info_text=info_text)
    await state.set_state(AddByCode.waiting_code)
    await message.answer(_code_entry_text(info_text, ""), reply_markup=digit_code_keyboard())


@router.message(AddByCode.waiting_code)
async def add_by_code_message_code(message: Message, state: FSMContext, config: Config) -> None:
    code = _normalize_login_code(message.text or "")
    await complete_code(message, state, config, code)


@router.callback_query(AddByCode.waiting_code, F.data.startswith("code:"))
async def add_by_code_digit(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    data = await state.get_data()
    code = data.get("code", "")
    info_text = data.get("code_info_text", "Код отправлен в Telegram. Введите код кнопками или одним сообщением.")

    if callback.data.startswith("code:digit:"):
        if len(code) >= 8:
            await callback.answer("Максимум 8 цифр.")
            return
        code += callback.data.rsplit(":", 1)[-1]
        await state.update_data(code=code)
        await callback.message.edit_text(_code_entry_text(info_text, code), reply_markup=digit_code_keyboard())
        await callback.answer()
        return

    if callback.data == "code:clear":
        if not code:
            await callback.answer("Код уже пустой.")
            return
        await state.update_data(code="")
        await callback.message.edit_text(_code_entry_text(info_text, ""), reply_markup=digit_code_keyboard())
        await callback.answer("Код очищен.")
        return

    if callback.data == "code:backspace":
        if not code:
            await callback.answer("Код уже пустой.")
            return
        code = code[:-1]
        await state.update_data(code=code)
        await callback.message.edit_text(_code_entry_text(info_text, code), reply_markup=digit_code_keyboard())
        await callback.answer()
        return

    if callback.data == "code:resend":
        await callback.answer("Повторный запрос отключен.", show_alert=True)
        return

    if callback.data == "code:done":
        await callback.answer()
        await complete_code(callback.message, state, config, code)


async def complete_code(message: Message, state: FSMContext, config: Config, code: str) -> None:
    code = _normalize_login_code(code)
    if not code:
        await message.answer("Код пустой.")
        return
    if not 5 <= len(code) <= 8:
        await message.answer("Код должен быть длиной 5-8 цифр. Введи заново.")
        return

    data = await state.get_data()
    phone_code_hash = data.get("phone_code_hash")
    if not phone_code_hash:
        await message.answer("Не найден phone_code_hash. Запроси код ещё раз.", reply_markup=digit_code_keyboard())
        return
    try:
        user = await sign_in_code(
            Path(data["session_path"]),
            data["phone"],
            code,
            phone_code_hash,
            config.telegram_api_id,
            config.telegram_api_hash,
            data["runtime"],
        )
    except SessionPasswordNeededError:
        await state.update_data(code=code)
        await state.set_state(AddByCode.waiting_twofa)
        await message.answer("Нужен пароль 2FA. Отправь пароль.")
        return
    except Exception as exc:
        await state.update_data(code="")
        await message.answer(f"Вход не удался: {exc}\nВведи код заново или запроси повторно.", reply_markup=digit_code_keyboard())
        return

    await finalize_code_login(message, state, config, twofa=None, user=user)


@router.message(AddByCode.waiting_twofa)
async def add_by_code_twofa(message: Message, state: FSMContext, config: Config) -> None:
    password = message.text or ""
    data = await state.get_data()
    try:
        user = await sign_in_password(
            Path(data["session_path"]),
            password,
            config.telegram_api_id,
            config.telegram_api_hash,
            data["runtime"],
        )
    except Exception as exc:
        await message.answer(f"Проверка 2FA не прошла: {exc}")
        return
    await finalize_code_login(message, state, config, twofa=password, user=user)


@router.callback_query(F.data == "accounts:add:session")
async def add_session_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddBySession.waiting_session)
    await state.update_data(expect_json=False)
    await callback.message.edit_text("Загрузи .session файл.")
    await callback.answer()


@router.callback_query(F.data == "accounts:add:session_json")
async def add_session_json_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddBySession.waiting_session)
    await state.update_data(expect_json=True)
    await callback.message.edit_text("Сначала загрузи .session файл.")
    await callback.answer()


@router.message(AddBySession.waiting_session, F.document)
async def add_session_file(message: Message, bot: Bot, state: FSMContext, config: Config) -> None:
    filename = message.document.file_name or ""
    if not filename.lower().endswith(".session"):
        await message.answer("Нужен файл с расширением .session.")
        return

    session_path = unique_path(config.sessions_dir, filename)
    await download_document(bot, message.document, session_path)
    data = await state.get_data()
    await state.update_data(session_path=str(session_path))

    if data.get("expect_json"):
        await state.set_state(AddBySession.waiting_json)
        await message.answer("Session сохранен. Загрузи JSON или импортируй без JSON.", reply_markup=skip_json_menu())
        return

    result = await import_session_account(
        config,
        session_path=session_path,
        source_type="session",
        created_by=message.from_user.id if message.from_user else None,
    )
    target_worker_id = _add_target_worker_id(data)
    _apply_added_account_destination(config, result.account_id, target_worker_id)
    await state.clear()
    await message.answer(
        (
            f"Session импортирован.\n"
            f"ID: {result.account_id}\n"
            f"Статус: {result.status}\n"
            f"Телефон: {result.phone or '-'}\n"
            f"Хранилище: {_add_target_label(config, target_worker_id)}\n"
            f"Раздел: НЕРЕГ"
        ),
        reply_markup=add_account_target_menu(list_workers(config)),
    )


@router.callback_query(AddBySession.waiting_json, F.data == "accounts:add:session:no_json")
async def session_import_without_json(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    data = await state.get_data()
    result = await import_session_account(
        config,
        session_path=Path(data["session_path"]),
        source_type="session",
        created_by=callback.from_user.id,
    )
    target_worker_id = _add_target_worker_id(data)
    _apply_added_account_destination(config, result.account_id, target_worker_id)
    await state.clear()
    await callback.message.edit_text(
        (
            f"Session импортирован без JSON.\n"
            f"ID: {result.account_id}\n"
            f"Статус: {result.status}\n"
            f"Телефон: {result.phone or '-'}\n"
            f"Хранилище: {_add_target_label(config, target_worker_id)}\n"
            f"Раздел: НЕРЕГ"
        ),
        reply_markup=add_account_target_menu(list_workers(config)),
    )
    await callback.answer()


@router.message(AddBySession.waiting_json, F.document)
async def add_json_file(message: Message, bot: Bot, state: FSMContext, config: Config) -> None:
    filename = message.document.file_name or ""
    if not filename.lower().endswith(".json"):
        await message.answer("Нужен файл с расширением .json.")
        return

    json_path = unique_path(config.json_dir, filename)
    await download_document(bot, message.document, json_path)
    data = await state.get_data()
    try:
        result = await import_session_account(
            config,
            session_path=Path(data["session_path"]),
            json_path=json_path,
            source_type="session_json",
            created_by=message.from_user.id if message.from_user else None,
        )
    except Exception as exc:
        await state.clear()
        await message.answer(
            f"Импорт не удался: {exc}",
            reply_markup=add_account_target_menu(list_workers(config)),
        )
        return

    await state.clear()
    target_worker_id = _add_target_worker_id(data)
    _apply_added_account_destination(config, result.account_id, target_worker_id)
    await message.answer(
        (
            f"Session + JSON импортированы.\n"
            f"ID: {result.account_id}\n"
            f"Статус: {result.status}\n"
            f"Телефон: {result.phone or '-'}\n"
            f"Хранилище: {_add_target_label(config, target_worker_id)}\n"
            f"Раздел: НЕРЕГ"
        ),
        reply_markup=add_account_target_menu(list_workers(config)),
    )


@router.callback_query(F.data == "accounts:add:zip")
async def add_zip_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddByZip.waiting_zip)
    await callback.message.edit_text("Загрузи .zip архив с .session и .json файлами.")
    await callback.answer()


@router.message(AddByZip.waiting_zip, F.document)
async def add_zip_file(message: Message, bot: Bot, state: FSMContext, config: Config) -> None:
    filename = message.document.file_name or ""
    if not filename.lower().endswith(".zip"):
        await message.answer("Нужен файл с расширением .zip.")
        return

    zip_path = unique_path(config.temp_dir, filename)
    await download_document(bot, message.document, zip_path)
    try:
        results, summary = await import_zip(
            config,
            zip_path=zip_path,
            created_by=message.from_user.id if message.from_user else None,
        )
    except Exception as exc:
        await state.clear()
        await message.answer(
            f"Импорт ZIP не удался: {exc}",
            reply_markup=add_account_target_menu(list_workers(config)),
        )
        return

    data = await state.get_data()
    target_worker_id = _add_target_worker_id(data)
    _apply_import_results_destination(config, results, target_worker_id)
    lines = [summary, ""]
    lines.append(f"Хранилище: {_add_target_label(config, target_worker_id)}")
    lines.append("Раздел: НЕРЕГ")
    lines.append("")
    for result in results[:20]:
        if result.account_id:
            lines.append(f"#{result.account_id} | {result.status} | {result.phone or result.username or '-'}")
        else:
            lines.append(f"ERROR | {result.note or 'unknown error'}")
    if len(results) > 20:
        lines.append(f"...и еще {len(results) - 20}")
    await state.clear()
    await message.answer("\n".join(lines), reply_markup=add_account_target_menu(list_workers(config)))


@router.message(F.text == "/cancel")
async def cancel(
    message: Message,
    state: FSMContext,
    config: Config,
    is_admin: bool,
    current_worker,
) -> None:
    await state.clear()
    if is_admin:
        await message.answer("Отменено.", reply_markup=main_menu())
        return
    await message.answer("Отменено.")
    await _show_worker_home_message(message, config, current_worker)


@router.callback_query(F.data.startswith("accounts:page:"))
async def show_accounts_page(callback: CallbackQuery, config: Config) -> None:
    _, _, origin, raw_ref, raw_page = callback.data.split(":", 4)
    await _show_account_page(callback, config, origin, int(raw_ref), int(raw_page))


@router.callback_query(F.data == "accounts:worker_storage")
async def show_worker_storage(callback: CallbackQuery, config: Config) -> None:
    workers = list_workers(config)
    if not workers:
        await callback.message.edit_text("Воркеров пока нет. Сначала добавь воркера.", reply_markup=accounts_menu())
        await callback.answer()
        return
    await callback.message.edit_text("Выбери воркера, чтобы открыть его хранилище.", reply_markup=worker_storage_keyboard(workers))
    await callback.answer()


@router.callback_query(F.data.startswith("worker:account_sections:"))
async def show_worker_account_sections(callback: CallbackQuery, config: Config) -> None:
    worker_id = int(callback.data.rsplit(":", 1)[-1])
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    _, nereg_count, reg_count = _worker_account_counts(config, worker)
    text = (
        f"Хранилище воркера\n{_worker_name(worker)}\n\n"
        f"НЕРЕГ: {nereg_count}\n"
        f"РЕГ: {reg_count}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=worker_account_sections_menu(worker_id, nereg_count=nereg_count, reg_count=reg_count),
    )
    await callback.answer()


@router.callback_query(F.data == "worker:self:menu")
async def show_worker_self_menu(callback: CallbackQuery, config: Config, current_worker) -> None:
    await _show_worker_home_callback(callback, config, current_worker)


@router.callback_query(F.data.startswith("worker:self:page:"))
async def show_worker_self_accounts(callback: CallbackQuery, config: Config, current_worker) -> None:
    _, _, _, stage, raw_page = callback.data.split(":", 4)
    if stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return

    worker_id, department_id = _worker_scope(current_worker)
    total = count_accounts_by_stage(
        config,
        worker_id=worker_id,
        department_id=department_id,
        account_stage=stage,
    )
    page = max(0, min(int(raw_page), _pages(total) - 1))
    accounts = list_accounts(
        config,
        limit=ACCOUNTS_PER_PAGE,
        offset=page * ACCOUNTS_PER_PAGE,
        worker_id=worker_id,
        department_id=department_id,
        account_stage=stage,
    )
    text = (
        f"Отдел: {_worker_department_title(config, current_worker)}\n"
        f"Раздел: {_worker_stage_title(stage)}\n\n"
    )
    if total:
        text += f"Всего: {total}\nСтраница: {page + 1}/{_pages(total)}"
    else:
        text += "Аккаунтов пока нет."
    await callback.message.edit_text(
        text,
        reply_markup=worker_self_accounts_page_keyboard(
            accounts,
            total=total,
            page=page,
            stage=stage,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("worker:self_account:"))
async def show_worker_self_account(callback: CallbackQuery, config: Config, current_worker) -> None:
    _, _, raw_account_id, stage, raw_page = callback.data.split(":", 4)
    account = get_account(config, int(raw_account_id))
    if not account or not _worker_can_access_account(account, current_worker):
        await callback.answer("Аккаунт недоступен.", show_alert=True)
        return
    await callback.message.edit_text(
        _worker_account_detail_text(account),
        reply_markup=worker_self_account_detail_menu(
            account.id,
            stage=stage,
            page=int(raw_page),
            account_stage=account.account_stage,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("worker:self_stage_ask1:"))
async def ask_worker_account_stage_first(callback: CallbackQuery, config: Config, current_worker) -> None:
    _, _, raw_account_id, target_stage, origin_stage, raw_page = callback.data.split(":", 5)
    account = get_account(config, int(raw_account_id))
    if not account or not _worker_can_access_account(account, current_worker):
        await callback.answer("Аккаунт недоступен.", show_alert=True)
        return
    if target_stage == "reg":
        text = "ВЫ УВЕРЕНЫ ЧТО ХОТИТЕ ПОМЕТИТЬ АКК РЕГАННЫМ?"
    else:
        text = "ВЫ УВЕРЕНЫ ЧТО ХОТИТЕ ПЕРЕНЕСТИ АКК В НЕРЕГ?"
    await callback.message.edit_text(
        text,
        reply_markup=confirm_worker_account_stage_menu(
            account.id,
            target_stage,
            origin_stage,
            int(raw_page),
            step=1,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("worker:self_stage_ask2:"))
async def ask_worker_account_stage_second(callback: CallbackQuery, config: Config, current_worker) -> None:
    _, _, raw_account_id, target_stage, origin_stage, raw_page = callback.data.split(":", 5)
    account = get_account(config, int(raw_account_id))
    if not account or not _worker_can_access_account(account, current_worker):
        await callback.answer("Аккаунт недоступен.", show_alert=True)
        return
    if target_stage == "reg":
        text = "ПОДТВЕРДИТЕ ЕЩЕ РАЗ: ПОМЕТИТЬ АКК РЕГАННЫМ?"
    else:
        text = "ПОДТВЕРДИТЕ ЕЩЕ РАЗ: ПЕРЕНЕСТИ АКК В НЕРЕГ?"
    await callback.message.edit_text(
        text,
        reply_markup=confirm_worker_account_stage_menu(
            account.id,
            target_stage,
            origin_stage,
            int(raw_page),
            step=2,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("worker:self_stage_confirm:"))
async def confirm_worker_account_stage(callback: CallbackQuery, config: Config, current_worker) -> None:
    _, _, raw_account_id, target_stage, _origin_stage, _raw_page = callback.data.split(":", 5)
    account_id = int(raw_account_id)
    account = get_account(config, account_id)
    if not account or not _worker_can_access_account(account, current_worker):
        await callback.answer("Аккаунт недоступен.", show_alert=True)
        return
    set_account_stage(config, account_id, target_stage)
    account = get_account(config, account_id)
    if not account or not _worker_can_access_account(account, current_worker):
        await callback.answer("Аккаунт недоступен.", show_alert=True)
        return
    new_stage = "reg" if account.account_stage == "reg" else "nereg"
    await callback.message.edit_text(
        _worker_account_detail_text(account),
        reply_markup=worker_self_account_detail_menu(
            account.id,
            stage=new_stage,
            page=0,
            account_stage=account.account_stage,
        ),
    )
    done = "Аккаунт перенесен в РЕГ." if target_stage == "reg" else "Аккаунт перенесен в НЕРЕГ."
    await callback.answer(done)


@router.callback_query(F.data.startswith("worker:self_phone:"))
async def send_worker_account_phone(callback: CallbackQuery, config: Config, current_worker) -> None:
    raw_account_id = callback.data.rsplit(":", 1)[-1]
    account = get_account(config, int(raw_account_id))
    if not account or not _worker_can_access_account(account, current_worker):
        await callback.answer("Аккаунт недоступен.", show_alert=True)
        return
    if not account.phone:
        await callback.answer("Номер не указан.", show_alert=True)
        return
    await callback.message.answer(_copyable(account.phone))
    await callback.answer("Номер отправлен.")


@router.callback_query(F.data.startswith("worker:self_code:"))
async def get_worker_self_account_code(callback: CallbackQuery, config: Config, current_worker) -> None:
    raw_account_id = callback.data.rsplit(":", 1)[-1]
    account = get_account(config, int(raw_account_id))
    if not account or not _worker_can_access_account(account, current_worker):
        await callback.answer("Аккаунт недоступен.", show_alert=True)
        return

    session_path = Path(account.session_path)
    if not session_path.exists():
        await callback.answer("Session файл не найден.", show_alert=True)
        return

    try:
        api_id, api_hash, runtime = _account_connection_params(account, config)
        code = await get_latest_telegram_code(session_path, api_id, api_hash, runtime)
    except Exception as exc:
        await callback.message.answer(f"Не удалось получить код из Verification Codes: {exc}")
        await callback.answer()
        return

    if not code:
        await callback.message.answer("Код не найден в последних сообщениях @VerificationCodes.")
        await callback.answer()
        return

    await callback.message.answer(f"Код из Verification Codes: <code>{code}</code>")
    await callback.answer("Код найден.")


@router.callback_query(F.data.startswith("accounts:phone:"))
async def send_account_phone(callback: CallbackQuery, config: Config) -> None:
    account_id = int(callback.data.rsplit(":", 1)[-1])
    account = get_account(config, account_id)
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    if not account.phone:
        await callback.answer("Номер не указан.", show_alert=True)
        return
    await callback.message.answer(_copyable(account.phone))
    await callback.answer("Номер отправлен.")


@router.callback_query(F.data.startswith("account:open:"))
async def show_account_detail_callback(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_id, origin, raw_ref, raw_page = callback.data.split(":", 5)
    account = get_account(config, int(raw_id))
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.message.edit_text(
        _account_detail_text(account, config),
        reply_markup=account_detail_menu(
            account.id,
            account_stage=account.account_stage,
            origin=origin,
            ref_id=int(raw_ref),
            page=int(raw_page),
        ),
    )
    await callback.answer()


@router.message(F.text.regexp(r"^/account_\d+$"))
async def show_account_detail_message(message: Message, config: Config) -> None:
    account_id = int((message.text or "").rsplit("_", 1)[-1])
    account = get_account(config, account_id)
    if not account:
        await message.answer("Аккаунт не найден.")
        return
    origin = "worker" if account.worker_id else "common"
    if account.worker_id:
        origin = "worker_reg" if account.account_stage == "reg" else "worker_nereg"
    else:
        origin = "common"
    ref_id = account.worker_id or 0
    await message.answer(
        _account_detail_text(account, config),
        reply_markup=account_detail_menu(account.id, account_stage=account.account_stage, origin=origin, ref_id=ref_id, page=0),
    )


@router.callback_query(F.data.startswith("accounts:file:"))
async def download_account_file(callback: CallbackQuery, config: Config) -> None:
    _, _, file_type, raw_id = callback.data.split(":", 3)
    account = get_account(config, int(raw_id))
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    path = Path(account.session_path) if file_type == "session" else Path(account.json_original_path or account.json_effective_path or "")
    if not path.exists():
        await callback.answer("Файл не найден.", show_alert=True)
        return

    await callback.message.answer_document(FSInputFile(path))
    await callback.answer()


@router.callback_query(F.data.startswith("account:assign:"))
async def assign_account_start(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_account_id, origin, raw_ref, raw_page = callback.data.split(":", 5)
    account = get_account(config, int(raw_account_id))
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    workers = list_workers(config)
    await callback.message.edit_text(
        "Выбери воркера для аккаунта.",
        reply_markup=assign_account_keyboard(
            workers,
            account_id=account.id,
            origin=origin,
            ref_id=int(raw_ref),
            page=int(raw_page),
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("account:assign_to:"))
async def assign_account_finish(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_account_id, raw_worker_id, origin, raw_ref, raw_page = callback.data.split(":", 6)
    account_id = int(raw_account_id)
    worker_id = int(raw_worker_id)
    if worker_id and not worker_exists(config, worker_id):
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    assign_account_to_worker(config, account_id, None if worker_id == 0 else worker_id)
    account = get_account(config, account_id)
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    if account.worker_id:
        new_origin = "worker_reg" if account.account_stage == "reg" else "worker_nereg"
    else:
        new_origin = "common"
    new_ref_id = account.worker_id or 0
    await callback.message.edit_text(
        _account_detail_text(account, config),
        reply_markup=account_detail_menu(
            account.id,
            account_stage=account.account_stage,
            origin=new_origin,
            ref_id=new_ref_id,
            page=0,
        ),
    )
    await callback.answer("Выдача аккаунта обновлена.")


@router.callback_query(F.data.startswith("account:stage_ask1:"))
async def ask_account_stage_first(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_account_id, target_stage, origin, raw_ref, raw_page = callback.data.split(":", 6)
    account = get_account(config, int(raw_account_id))
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    if target_stage == "reg":
        text = "ВЫ УВЕРЕНЫ ЧТО ХОТИТЕ ПОМЕТИТЬ АКК РЕГАННЫМ?"
    else:
        text = "ВЫ УВЕРЕНЫ ЧТО ХОТИТЕ ПЕРЕНЕСТИ АКК В НЕРЕГ?"
    await callback.message.edit_text(
        text,
        reply_markup=confirm_account_stage_menu(
            account.id,
            target_stage,
            origin,
            int(raw_ref),
            int(raw_page),
            step=1,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("account:stage_ask2:"))
async def ask_account_stage_second(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_account_id, target_stage, origin, raw_ref, raw_page = callback.data.split(":", 6)
    account = get_account(config, int(raw_account_id))
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    if target_stage == "reg":
        text = "ПОДТВЕРДИТЕ ЕЩЕ РАЗ: ПОМЕТИТЬ АКК РЕГАННЫМ?"
    else:
        text = "ПОДТВЕРДИТЕ ЕЩЕ РАЗ: ПЕРЕНЕСТИ АКК В НЕРЕГ?"
    await callback.message.edit_text(
        text,
        reply_markup=confirm_account_stage_menu(
            account.id,
            target_stage,
            origin,
            int(raw_ref),
            int(raw_page),
            step=2,
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("account:stage_confirm:"))
async def confirm_account_stage(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_account_id, target_stage, origin, raw_ref, raw_page = callback.data.split(":", 6)
    account_id = int(raw_account_id)
    account = get_account(config, account_id)
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    set_account_stage(config, account_id, target_stage)
    account = get_account(config, account_id)
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    if account.worker_id:
        new_origin = "worker_reg" if account.account_stage == "reg" else "worker_nereg"
        new_ref_id = account.worker_id
    else:
        new_origin = origin
        new_ref_id = int(raw_ref)
    await callback.message.edit_text(
        _account_detail_text(account, config),
        reply_markup=account_detail_menu(
            account.id,
            account_stage=account.account_stage,
            origin=new_origin,
            ref_id=new_ref_id,
            page=0,
        ),
    )
    done = "Аккаунт перенесен в РЕГ." if target_stage == "reg" else "Аккаунт перенесен в НЕРЕГ."
    await callback.answer(done)


@router.callback_query(F.data == "departments:menu")
async def show_departments(callback: CallbackQuery, config: Config) -> None:
    departments = list_departments(config)
    text = "Отделы"
    if not departments:
        text += "\n\nОтделов пока нет."
    await callback.message.edit_text(text, reply_markup=departments_menu(departments))
    await callback.answer()


@router.callback_query(F.data == "departments:add")
async def add_department_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CreateDepartment.waiting_name)
    await callback.message.edit_text("Отправь название отдела.")
    await callback.answer()


@router.message(CreateDepartment.waiting_name)
async def add_department_finish(message: Message, state: FSMContext, config: Config) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название пустое.")
        return
    department_id = add_department(config, name, utc_now_iso())
    await state.clear()
    await message.answer(f"Отдел создан: {name}", reply_markup=departments_menu(list_departments(config)))


@router.callback_query(F.data.startswith("department:open:"))
async def open_department(callback: CallbackQuery, config: Config) -> None:
    department_id = int(callback.data.rsplit(":", 1)[-1])
    department = get_department(config, department_id)
    if not department:
        await callback.answer("Отдел не найден.", show_alert=True)
        return
    workers_count = sum(1 for worker in list_workers(config) if worker["department_id"] == department_id)
    text = f"Отдел\nНазвание: {department['name']}\nВоркеров: {workers_count}"
    await callback.message.edit_text(text, reply_markup=department_detail_menu(department_id))
    await callback.answer()


@router.callback_query(F.data.startswith("department:delete:ask:"))
async def ask_delete_department(callback: CallbackQuery, config: Config) -> None:
    department_id = int(callback.data.rsplit(":", 1)[-1])
    department = get_department(config, department_id)
    if not department:
        await callback.answer("Отдел не найден.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Удалить отдел «{department['name']}»?\nСвязанные воркеры останутся, но будут без отдела.",
        reply_markup=confirm_delete_department_menu(department_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("department:delete:confirm:"))
async def confirm_delete_department(callback: CallbackQuery, config: Config) -> None:
    department_id = int(callback.data.rsplit(":", 1)[-1])
    delete_department(config, department_id)
    await callback.message.edit_text("Отдел удален.", reply_markup=departments_menu(list_departments(config)))
    await callback.answer()


@router.callback_query(F.data == "workers:menu")
async def show_workers(callback: CallbackQuery, config: Config) -> None:
    workers = list_workers(config)
    text = "Воркеры"
    if not workers:
        text += "\n\nВоркеров пока нет."
    await callback.message.edit_text(text, reply_markup=workers_menu(workers))
    await callback.answer()


@router.callback_query(F.data == "workers:add")
async def add_worker_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CreateWorker.waiting_telegram_id)
    await callback.message.edit_text("Отправь Telegram ID воркера.")
    await callback.answer()


@router.message(CreateWorker.waiting_telegram_id)
async def add_worker_choose_department(message: Message, state: FSMContext, config: Config) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Telegram ID должен быть числом.")
        return
    await state.update_data(worker_telegram_id=int(raw))
    departments = list_departments(config)
    await message.answer(
        "Теперь выбери отдел, к которому у воркера будет доступ.",
        reply_markup=worker_department_select_menu(departments),
    )


@router.callback_query(CreateWorker.waiting_telegram_id, F.data.startswith("worker:add:dept:"))
async def add_worker_finish(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    department_id = int(callback.data.rsplit(":", 1)[-1])
    if department_id and not get_department(config, department_id):
        await callback.answer("Отдел не найден.", show_alert=True)
        return
    data = await state.get_data()
    telegram_id = int(data["worker_telegram_id"])
    worker_id = add_worker(
        config,
        name=f"Воркер {telegram_id}",
        telegram_id=telegram_id,
        department_id=None if department_id == 0 else department_id,
        created_at=utc_now_iso(),
    )
    department = get_department(config, department_id) if department_id else None
    await state.clear()
    await callback.message.edit_text(
        f"Воркер создан\nTelegram ID: {telegram_id}\nОтдел: {department['name'] if department else '-'}",
        reply_markup=workers_menu(list_workers(config)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("worker:open:"))
async def open_worker(callback: CallbackQuery, config: Config) -> None:
    worker_id = int(callback.data.rsplit(":", 1)[-1])
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    department = get_department(config, worker["department_id"]) if worker["department_id"] else None
    accounts_total, nereg_count, reg_count = _worker_account_counts(config, worker)
    text = (
        f"Воркер\n"
        f"Имя: {worker['name']}\n"
        f"Telegram ID: {worker['telegram_id'] or '-'}\n"
        f"Отдел: {department['name'] if department else '-'}\n"
        f"Аккаунтов: {accounts_total}\n"
        f"НЕРЕГ: {nereg_count}\n"
        f"РЕГ: {reg_count}"
    )
    await callback.message.edit_text(text, reply_markup=worker_detail_menu(worker_id))
    await callback.answer()


@router.callback_query(F.data.startswith("worker:delete:ask:"))
async def ask_delete_worker(callback: CallbackQuery, config: Config) -> None:
    worker_id = int(callback.data.rsplit(":", 1)[-1])
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Удалить воркера «{worker['name']}»?\nЕго аккаунты и прокси вернутся в общие хранилища.",
        reply_markup=confirm_delete_worker_menu(worker_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("worker:delete:confirm:"))
async def confirm_delete_worker(callback: CallbackQuery, config: Config) -> None:
    worker_id = int(callback.data.rsplit(":", 1)[-1])
    delete_worker(config, worker_id)
    await callback.message.edit_text("Воркер удален.", reply_markup=workers_menu(list_workers(config)))
    await callback.answer()


@router.callback_query(F.data == "proxies:menu")
async def show_proxies(callback: CallbackQuery, config: Config) -> None:
    rows = list_proxies(config)
    text = "Прокси"
    if rows:
        text += "\n\n" + "\n".join(
            f"#{row['id']} | воркер:{row['worker_id'] or '-'} | {row['proxy']}"
            for row in rows
        )
        text += "\n\nОтправь /proxy_<id>, чтобы выдать прокси воркеру."
    else:
        text += "\n\nПрокси пока нет."
    await callback.message.edit_text(text, reply_markup=proxies_menu())
    await callback.answer()


@router.callback_query(F.data == "proxies:add")
async def add_proxy_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddProxy.waiting_text)
    await callback.message.edit_text("Отправь прокси списком, по одному в строке.")
    await callback.answer()


@router.message(AddProxy.waiting_text)
async def add_proxy_finish(message: Message, state: FSMContext, config: Config) -> None:
    lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
    if not lines:
        await message.answer("Список прокси пустой.")
        return
    now = utc_now_iso()
    ids = [add_proxy(config, line, now) for line in lines]
    await state.clear()
    await message.answer(f"Добавлено прокси: {len(ids)}", reply_markup=proxies_menu())


@router.message(F.text.regexp(r"^/proxy_\d+$"))
async def assign_proxy_start(message: Message, state: FSMContext, config: Config) -> None:
    proxy_id = int((message.text or "").rsplit("_", 1)[-1])
    if not proxy_exists(config, proxy_id):
        await message.answer("Прокси не найден.")
        return
    workers = list_workers(config)
    lines = [f"#{row['id']} | {row['name']}" for row in workers]
    await state.set_state(AssignProxy.waiting_worker_id)
    await state.update_data(proxy_id=proxy_id)
    text = "Отправь ID воркера для этого прокси или 0, чтобы вернуть в общее хранилище прокси."
    if lines:
        text += "\n\nВоркеры:\n" + "\n".join(lines)
    await message.answer(text)


@router.message(AssignProxy.waiting_worker_id)
async def assign_proxy_finish(message: Message, state: FSMContext, config: Config) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Отправь числовой ID воркера или 0.")
        return
    worker_id = int(raw)
    if worker_id != 0 and not worker_exists(config, worker_id):
        await message.answer("Воркер не найден.")
        return
    data = await state.get_data()
    assign_proxy_to_worker(config, int(data["proxy_id"]), None if worker_id == 0 else worker_id)
    await state.clear()
    await message.answer("Выдача прокси обновлена.", reply_markup=proxies_menu())


@router.callback_query(F.data == "settings:menu")
async def show_settings(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Настройки\n\nКонфиг загружается из .env.",
        reply_markup=placeholder_menu("settings"),
    )
    await callback.answer()
