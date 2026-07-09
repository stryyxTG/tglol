from __future__ import annotations

from html import escape
from math import ceil
from pathlib import Path
import re
import secrets
import shutil
import time
import zipfile

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message, TelegramObject
from telethon.errors import SessionPasswordNeededError

from tglol.config import Config
from tglol.db import (
    add_proxy,
    add_worker,
    assign_account_to_worker,
    assign_available_proxies_to_worker,
    assign_common_accounts_to_worker,
    count_available_proxies,
    count_accounts_by_stage,
    count_worker_proxies,
    delete_account_row,
    delete_accounts_by_scope,
    delete_worker,
    get_account,
    get_worker,
    get_worker_by_telegram_id,
    list_accounts,
    list_accounts_by_scope,
    list_available_proxies,
    list_workers,
    move_accounts_to_common_by_scope,
    pop_worker_proxy,
    set_account_worker_and_stage,
    set_account_stage,
    update_worker_name,
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
    admin_reply_menu,
    add_account_menu,
    add_account_target_menu,
    assign_account_keyboard,
    bulk_assign_account_amount_keyboard,
    bulk_assign_account_worker_keyboard,
    common_storage_sections_menu,
    common_target_stage_menu,
    confirm_bulk_return_menu,
    confirm_account_stage_menu,
    confirm_delete_account_menu,
    confirm_delete_common_stage_menu,
    confirm_delete_worker_stage_menu,
    confirm_worker_account_stage_menu,
    confirm_delete_worker_menu,
    digit_code_keyboard,
    download_zip_amount_keyboard,
    main_menu,
    placeholder_menu,
    proxies_storage_keyboard,
    proxy_amount_keyboard,
    proxy_detail_keyboard,
    proxy_worker_select_keyboard,
    proxies_menu,
    skip_json_menu,
    worker_proxy_menu,
    worker_account_sections_menu,
    worker_detail_menu,
    worker_name_choice_menu,
    worker_reply_menu,
    worker_self_account_detail_menu,
    worker_self_accounts_page_keyboard,
    worker_self_menu,
    worker_storage_keyboard,
    workers_menu,
)
from tglol.paths import unique_path
from tglol.states import (
    AddByCode,
    AddBySession,
    AddByZip,
    AddProxy,
    AssignAccount,
    AssignProxy,
    CreateWorker,
    DownloadAccountsZip,
    RenameWorker,
)
from tglol.telegram_service import get_latest_telegram_code, send_code, sign_in_code, sign_in_password, user_fields

router = Router()

WORKER_CODE_MESSAGES: dict[int, dict[str, object]] = {}


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
                if text.startswith("/start") or text in {"/cancel", "Рабочая панель"}:
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
                    or callback_data.startswith("worker:self_proxy:")
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


def _format_phone(value) -> str | None:
    if value in (None, ""):
        return None
    phone = str(value).strip()
    digits = re.sub(r"\D+", "", phone)
    if len(digits) == 11 and digits.startswith("7"):
        return f"{digits[0]} {digits[1:4]} {digits[4:7]} {digits[7:]}"
    return phone


def _account_name(account) -> str:
    return str(_format_phone(account.phone) or account.username or account.telegram_user_id or "без данных")


def _text(value) -> str:
    return escape(str(value)) if value not in (None, "") else "-"


def _copyable(value) -> str:
    return f"<code>{escape(str(value))}</code>" if value not in (None, "") else "-"


def _bold_value(value) -> str:
    return f"<b>{escape(str(value))}</b>" if value not in (None, "") else "-"


async def _clear_worker_code_messages(bot: Bot, chat_id: int) -> None:
    state = WORKER_CODE_MESSAGES.pop(chat_id, None)
    if not state:
        return
    for message_id in state.get("message_ids", []):
        try:
            await bot.delete_message(chat_id, int(message_id))
        except Exception:
            pass


async def _clear_worker_code_messages_if_account_changed(bot: Bot, chat_id: int, account_id: int) -> None:
    state = WORKER_CODE_MESSAGES.get(chat_id)
    if state and state.get("account_id") != account_id:
        await _clear_worker_code_messages(bot, chat_id)


def _remember_worker_code_message(chat_id: int, account_id: int, message: Message) -> None:
    state = WORKER_CODE_MESSAGES.setdefault(chat_id, {"account_id": account_id, "message_ids": []})
    state["account_id"] = account_id
    message_ids = state.setdefault("message_ids", [])
    if isinstance(message_ids, list):
        message_ids.append(message.message_id)


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


async def _fetch_worker_telegram_name(bot: Bot, telegram_id: int) -> str | None:
    try:
        chat = await bot.get_chat(telegram_id)
    except Exception:
        return None
    parts = [
        getattr(chat, "first_name", None),
        getattr(chat, "last_name", None),
    ]
    name = " ".join(str(part).strip() for part in parts if part).strip()
    return name or None


def _clean_worker_label(raw: str) -> str | None:
    label = " ".join((raw or "").split())
    if not label or label in {"-", "0"}:
        return None
    return label[:64]


def _worker_scope(worker) -> tuple[int | None | str, int | None | str]:
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


def _worker_detail_text(config: Config, worker) -> str:
    accounts_total, nereg_count, reg_count = _worker_account_counts(config, worker)
    return (
        f"Воркер\n"
        f"Имя: {escape(worker['name'])}\n"
        f"Telegram ID: {worker['telegram_id'] or '-'}\n"
        f"Аккаунтов: {accounts_total}\n"
        f"НЕРЕГ: {nereg_count}\n"
        f"РЕГ: {reg_count}"
    )


def _worker_can_access_account(account, worker) -> bool:
    return account.worker_id == worker["id"]


def _worker_stage_title(stage: str) -> str:
    return "РЕГ" if stage == "reg" else "НЕРЕГ"


def _common_account_counts(config: Config) -> tuple[int, int]:
    nereg_count = count_accounts_by_stage(config, worker_id=None, account_stage="nereg")
    reg_count = count_accounts_by_stage(config, worker_id=None, account_stage="reg")
    return nereg_count, reg_count


def _stage_title(stage: str) -> str:
    return "РЕГ" if stage == "reg" else "НЕРЕГ"


def _account_file_paths(account) -> list[Path]:
    paths: list[Path] = []
    for raw in (account.session_path, account.json_original_path, account.json_effective_path):
        if raw:
            path = Path(raw)
            if path not in paths:
                paths.append(path)
    return paths


def _resolved_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_allowed_storage_file(config: Config, path: Path) -> bool:
    resolved = _resolved_path(path)
    allowed_roots = (config.data_dir, config.sessions_dir, config.json_dir, config.temp_dir)
    for root in allowed_roots:
        try:
            resolved.relative_to(_resolved_path(root))
            return path.exists() and path.is_file()
        except ValueError:
            continue
    return False


def _delete_local_account_files(config: Config, accounts: list) -> int:
    selected_ids = {account.id for account in accounts}
    remaining_paths = {
        str(_resolved_path(path))
        for account in list_accounts_by_scope(config)
        if account.id not in selected_ids
        for path in _account_file_paths(account)
    }
    removed = 0
    seen: set[str] = set()
    for account in accounts:
        for path in _account_file_paths(account):
            resolved = str(_resolved_path(path))
            if resolved in seen or resolved in remaining_paths:
                continue
            seen.add(resolved)
            if not _is_allowed_storage_file(config, path):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def _make_accounts_zip(config: Config, accounts: list, stage: str) -> tuple[Path, int]:
    zip_path = unique_path(config.temp_dir, f"common_{stage}_{secrets.token_hex(4)}.zip")
    files_count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive_names: set[str] = set()
        for account in accounts:
            added_paths: set[str] = set()
            for path in _account_file_paths(account):
                resolved = str(_resolved_path(path))
                if resolved in added_paths or not path.exists() or not path.is_file():
                    continue
                added_paths.add(resolved)
                archive_name = path.name
                if archive_name in archive_names:
                    archive_name = f"{account.id}_{path.name}"
                archive_names.add(archive_name)
                archive.write(path, arcname=archive_name)
                files_count += 1
    return zip_path, files_count


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


async def _show_worker_home_callback(
    callback: CallbackQuery,
    config: Config,
    worker,
    *,
    answer_text: str | None = None,
) -> None:
    nereg_count, reg_count = _worker_counts(config, worker)
    text = (
        f"📲Рабочая панель\n"
        f"🧑🏻‍💻Воркер : {escape(_worker_name(worker))}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=worker_self_menu(
            nereg_count=nereg_count,
            reg_count=reg_count,
        ),
    )
    await callback.answer(answer_text)


async def _show_worker_home_message(message: Message, config: Config, worker) -> None:
    nereg_count, reg_count = _worker_counts(config, worker)
    text = (
        f"📲Рабочая панель\n"
        f"🧑🏻‍💻Воркер : {escape(_worker_name(worker))}"
    )
    await message.answer(
        text,
        reply_markup=worker_self_menu(
            nereg_count=nereg_count,
            reg_count=reg_count,
        ),
    )


async def _show_account_page(callback: CallbackQuery, config: Config, origin: str, ref_id: int, page: int) -> None:
    worker = None
    is_common = origin in {"common", "common_nereg", "common_reg"}
    worker_filter: int | None | str = None if is_common else ref_id
    department_filter: int | None | str = "any"
    account_stage = None
    if origin in {"worker_nereg", "common_nereg"}:
        account_stage = "nereg"
    elif origin in {"worker_reg", "common_reg"}:
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
    elif origin in {"common_nereg", "common_reg"}:
        title = f"Общее хранилище\nРаздел: {_stage_title(account_stage or 'nereg')}"
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
    stage = "РЕГ" if account.account_stage == "reg" else "НЕРЕГ"
    return (
        f"<b>Аккаунт #{account.id}</b> · {stage}\n"
        f"Статус: <code>{_text(account.status)}</code>\n\n"
        f"Телефон:\n{_bold_value(_format_phone(account.phone))}\n\n"
        f"Username: {_username(account.username)}\n"
        f"User ID: {_copyable(account.telegram_user_id)}\n\n"
        f"JSON: {_text(account.json_source)}\n"
        f"Источник: {_text(account.source_type)}\n"
        f"Воркер: {_text(_worker_name(worker))}"
    )


def _worker_account_detail_text(account) -> str:
    stage = "РЕГ" if account.account_stage == "reg" else "НЕРЕГ"
    return (
        f"<b>Аккаунт #{account.id}</b> · {stage}\n"
        f"Статус: <code>{_text(account.status)}</code>\n\n"
        f"Телефон:\n{_bold_value(_format_phone(account.phone))}\n\n"
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
        reply_markup=accounts_menu(),
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
        await message.answer("Кнопка админ-панели включена.", reply_markup=admin_reply_menu())
        await message.answer("Админ-панель", reply_markup=main_menu())
        return
    await message.answer("Кнопка рабочей панели включена.", reply_markup=worker_reply_menu())
    await _show_worker_home_message(message, config, current_worker)


@router.message(F.text == "Админ панель")
async def admin_panel_button(message: Message, state: FSMContext, is_admin: bool) -> None:
    await state.clear()
    if not is_admin:
        await message.answer("Нет доступа.")
        return
    await message.answer("Админ-панель", reply_markup=main_menu())


@router.message(F.text == "Рабочая панель")
async def worker_panel_button(message: Message, state: FSMContext, config: Config, current_worker) -> None:
    await state.clear()
    if not current_worker:
        await message.answer("Нет доступа.")
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


@router.callback_query(F.data == "accounts:common_sections")
async def show_common_account_sections(callback: CallbackQuery, config: Config) -> None:
    nereg_count, reg_count = _common_account_counts(config)
    text = (
        "Общее хранилище\n\n"
        f"НЕРЕГ: {nereg_count}\n"
        f"РЕГ: {reg_count}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=common_storage_sections_menu(nereg_count=nereg_count, reg_count=reg_count),
    )
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
        reply_markup=accounts_menu(),
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
        reply_markup=accounts_menu(),
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
        reply_markup=accounts_menu(),
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
    progress_message = await message.answer("ZIP получен. Начинаю импорт...")
    last_progress_update = 0.0

    async def update_zip_progress(done: int, total: int, current: str) -> None:
        nonlocal last_progress_update
        now = time.monotonic()
        if done not in {0, total} and done % 5 != 0 and now - last_progress_update < 3:
            return
        last_progress_update = now
        current_line = f"\nСейчас: {current}" if current else ""
        try:
            await progress_message.edit_text(
                f"Импорт ZIP...\nГотово: {done}/{total}{current_line}"
            )
        except Exception:
            pass

    try:
        results, summary = await import_zip(
            config,
            zip_path=zip_path,
            created_by=message.from_user.id if message.from_user else None,
            progress=update_zip_progress,
        )
    except Exception as exc:
        await state.clear()
        try:
            await progress_message.edit_text(f"Импорт ZIP не удался: {exc}")
        except Exception:
            pass
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
    try:
        await progress_message.edit_text("Импорт ZIP завершён.")
    except Exception:
        pass
    await message.answer("\n".join(lines), reply_markup=accounts_menu())


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
async def show_accounts_page(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    await state.clear()
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
        f"Воркер: {_worker_name(current_worker)}\n"
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
async def show_worker_self_account(callback: CallbackQuery, bot: Bot, config: Config, current_worker) -> None:
    _, _, raw_account_id, stage, raw_page = callback.data.split(":", 4)
    account = get_account(config, int(raw_account_id))
    if not account or not _worker_can_access_account(account, current_worker):
        await callback.answer("Аккаунт недоступен.", show_alert=True)
        return
    await _clear_worker_code_messages_if_account_changed(bot, callback.message.chat.id, account.id)
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
    done = "Аккаунт перенесен в РЕГ." if target_stage == "reg" else "Аккаунт перенесен в НЕРЕГ."
    await _show_worker_home_callback(callback, config, current_worker, answer_text=done)


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
    await callback.message.answer(_bold_value(_format_phone(account.phone)))
    await callback.answer("Номер отправлен.")


@router.callback_query(F.data.startswith("worker:self_code:"))
async def get_worker_self_account_code(callback: CallbackQuery, bot: Bot, config: Config, current_worker) -> None:
    raw_account_id = callback.data.rsplit(":", 1)[-1]
    account = get_account(config, int(raw_account_id))
    if not account or not _worker_can_access_account(account, current_worker):
        await callback.answer("Аккаунт недоступен.", show_alert=True)
        return
    await _clear_worker_code_messages(bot, callback.message.chat.id)

    session_path = Path(account.session_path)
    if not session_path.exists():
        await callback.answer("Session файл не найден.", show_alert=True)
        return

    try:
        api_id, api_hash, runtime = _account_connection_params(account, config)
        code = await get_latest_telegram_code(session_path, api_id, api_hash, runtime)
    except Exception as exc:
        sent = await callback.message.answer(f"Не удалось получить код из Verification Codes: {exc}")
        _remember_worker_code_message(callback.message.chat.id, account.id, sent)
        await callback.answer()
        return

    if not code:
        sent = await callback.message.answer("Код не найден в последних сообщениях @VerificationCodes.")
        _remember_worker_code_message(callback.message.chat.id, account.id, sent)
        await callback.answer()
        return

    sent = await callback.message.answer(f"Код из Verification Codes: <code>{code}</code>")
    _remember_worker_code_message(callback.message.chat.id, account.id, sent)
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
    await callback.message.answer(_bold_value(_format_phone(account.phone)))
    await callback.answer("Номер отправлен.")


@router.callback_query(F.data.startswith("account:code:"))
async def get_account_verification_code(callback: CallbackQuery, config: Config) -> None:
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer("Неверный запрос.", show_alert=True)
        return
    account_id = int(parts[2]) if len(parts) > 2 else int(parts[1])
    account = get_account(config, account_id)
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    if not account.phone:
        await callback.answer("Номер не указан.", show_alert=True)
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
        origin = "common_reg" if account.account_stage == "reg" else "common_nereg"
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


async def _send_common_stage_zip(message: Message, config: Config, stage: str, amount: int) -> tuple[str, bool]:
    if stage not in {"nereg", "reg"}:
        return "Неизвестный раздел.", False
    total = count_accounts_by_stage(config, worker_id=None, account_stage=stage)
    if total <= 0:
        return "В этом разделе нет аккаунтов.", False
    if amount <= 0:
        return "Количество должно быть больше нуля.", False
    amount = min(amount, total)
    accounts = list_accounts(
        config,
        limit=amount,
        offset=0,
        worker_id=None,
        account_stage=stage,
    )
    if not accounts:
        return "В этом разделе нет аккаунтов.", False
    zip_path, files_count = _make_accounts_zip(config, accounts, stage)
    if files_count == 0:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
        return "Файлы для архива не найдены.", False
    await message.answer_document(
        FSInputFile(zip_path),
        caption=(
            f"Общее хранилище {_stage_title(stage)}: "
            f"выгружено {len(accounts)} из {total} аккаунтов, {files_count} файлов."
        ),
    )
    try:
        zip_path.unlink(missing_ok=True)
    except OSError:
        pass
    return "ZIP сформирован.", True


@router.callback_query(F.data.startswith("accounts:zip_common:"))
async def download_common_stage_zip_start(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    stage = callback.data.rsplit(":", 1)[-1]
    if stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    total = count_accounts_by_stage(config, worker_id=None, account_stage=stage)
    if not total:
        await callback.answer("В этом разделе нет аккаунтов.", show_alert=True)
        return
    await state.set_state(DownloadAccountsZip.waiting_amount)
    await state.update_data(download_zip_stage=stage)
    await callback.message.edit_text(
        (
            f"Скачать ZIP из общего {_stage_title(stage)}\n"
            f"Доступно аккаунтов: {total}\n\n"
            "Выбери количество или отправь число."
        ),
        reply_markup=download_zip_amount_keyboard(total, stage),
    )
    await callback.answer()


@router.callback_query(DownloadAccountsZip.waiting_amount, F.data.startswith("accounts:zip_amount:"))
async def download_common_stage_zip_amount_button(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    _, _, stage, raw_amount = callback.data.split(":", 3)
    total = count_accounts_by_stage(config, worker_id=None, account_stage=stage)
    amount = total if raw_amount == "all" else int(raw_amount)
    text, done = await _send_common_stage_zip(callback.message, config, stage, amount)
    await state.clear()
    nereg_count, reg_count = _common_account_counts(config)
    await callback.message.edit_text(
        text,
        reply_markup=common_storage_sections_menu(nereg_count=nereg_count, reg_count=reg_count),
    )
    await callback.answer("ZIP сформирован." if done else text, show_alert=not done)


@router.message(DownloadAccountsZip.waiting_amount)
async def download_common_stage_zip_amount_message(message: Message, state: FSMContext, config: Config) -> None:
    raw = (message.text or "").strip().lower()
    data = await state.get_data()
    stage = data.get("download_zip_stage")
    if stage not in {"nereg", "reg"}:
        await state.clear()
        await message.answer("Раздел выгрузки потерян. Начни заново.")
        return
    total = count_accounts_by_stage(config, worker_id=None, account_stage=stage)
    if raw in {"all", "все", "всё"}:
        amount = total
    elif raw.isdigit():
        amount = int(raw)
    else:
        await message.answer("Отправь количество числом или напиши: все.")
        return
    text, done = await _send_common_stage_zip(message, config, stage, amount)
    if done:
        await state.clear()
        nereg_count, reg_count = _common_account_counts(config)
        await message.answer(
            text,
            reply_markup=common_storage_sections_menu(nereg_count=nereg_count, reg_count=reg_count),
        )
        return
    await message.answer(text)


@router.callback_query(F.data.startswith("account:return_common:"))
async def return_account_common_start(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_account_id, origin, raw_ref, raw_page = callback.data.split(":", 5)
    account = get_account(config, int(raw_account_id))
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    if not account.worker_id:
        await callback.answer("Аккаунт уже в общем хранилище.", show_alert=True)
        return
    prefix = f"account:return_common_to:{account.id}:{origin}:{raw_ref}:{raw_page}"
    cancel = f"account:open:{account.id}:{origin}:{raw_ref}:{raw_page}"
    await callback.message.edit_text(
        "Куда вернуть аккаунт в общем хранилище?",
        reply_markup=common_target_stage_menu(prefix, cancel),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("account:return_common_to:"))
async def return_account_common_finish(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_account_id, _origin, _raw_ref, _raw_page, target_stage = callback.data.split(":", 6)
    if target_stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    account_id = int(raw_account_id)
    account = get_account(config, account_id)
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    set_account_worker_and_stage(config, account_id, None, target_stage)
    account = get_account(config, account_id)
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    new_origin = f"common_{target_stage}"
    await callback.message.edit_text(
        _account_detail_text(account, config),
        reply_markup=account_detail_menu(
            account.id,
            account_stage=account.account_stage,
            origin=new_origin,
            ref_id=0,
            page=0,
        ),
    )
    await callback.answer(f"Аккаунт возвращен в общий {_stage_title(target_stage)}.")


@router.callback_query(F.data.startswith("account:delete_ask:"))
async def ask_delete_account(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_account_id, origin, raw_ref, raw_page = callback.data.split(":", 5)
    account = get_account(config, int(raw_account_id))
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    if origin in {"common", "common_nereg", "common_reg"} and account.worker_id is not None:
        await callback.answer("Этот аккаунт уже не в общем хранилище.", show_alert=True)
        return
    if origin in {"worker", "worker_nereg", "worker_reg"} and account.worker_id != int(raw_ref):
        await callback.answer("Этот аккаунт уже не у выбранного воркера.", show_alert=True)
        return
    if origin not in {"common", "common_nereg", "common_reg", "worker", "worker_nereg", "worker_reg"}:
        await callback.answer("Удаление здесь недоступно.", show_alert=True)
        return
    await callback.message.edit_text(
        "ВЫ УВЕРЕНЫ ЧТО ХОТИТЕ УДАЛИТЬ АККАУНТ И ЕГО ФАЙЛЫ С СЕРВЕРА?",
        reply_markup=confirm_delete_account_menu(account.id, origin, int(raw_ref), int(raw_page)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("account:delete_confirm:"))
async def confirm_delete_account(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_account_id, origin, raw_ref, _raw_page = callback.data.split(":", 5)
    account = get_account(config, int(raw_account_id))
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    if origin in {"common", "common_nereg", "common_reg"} and account.worker_id is not None:
        await callback.answer("Этот аккаунт уже не в общем хранилище.", show_alert=True)
        return
    if origin in {"worker", "worker_nereg", "worker_reg"} and account.worker_id != int(raw_ref):
        await callback.answer("Этот аккаунт уже не у выбранного воркера.", show_alert=True)
        return
    if origin not in {"common", "common_nereg", "common_reg", "worker", "worker_nereg", "worker_reg"}:
        await callback.answer("Удаление здесь недоступно.", show_alert=True)
        return
    stage = account.account_stage
    removed_files = _delete_local_account_files(config, [account])
    delete_account_row(config, account.id)
    if origin in {"worker", "worker_nereg", "worker_reg"}:
        worker_id = int(raw_ref)
        worker = get_worker(config, worker_id)
        if not worker:
            await callback.message.edit_text(
                f"Аккаунт #{account.id} удален из бота.\nФайлов удалено с сервера: {removed_files}",
                reply_markup=accounts_menu(),
            )
            await callback.answer(f"Удалено из {_stage_title(stage)}.")
            return
        _, nereg_count, reg_count = _worker_account_counts(config, worker)
        await callback.message.edit_text(
            (
                f"Аккаунт #{account.id} удален из хранилища воркера.\n"
                f"Файлов удалено с сервера: {removed_files}\n\n"
                f"Хранилище воркера\n{_worker_name(worker)}\n\n"
                f"НЕРЕГ: {nereg_count}\n"
                f"РЕГ: {reg_count}"
            ),
            reply_markup=worker_account_sections_menu(worker_id, nereg_count=nereg_count, reg_count=reg_count),
        )
        await callback.answer(f"Удалено из {_stage_title(stage)}.")
        return
    nereg_count, reg_count = _common_account_counts(config)
    await callback.message.edit_text(
        (
            f"Аккаунт #{account.id} удален из бота.\n"
            f"Файлов удалено с сервера: {removed_files}\n\n"
            f"Общее хранилище\nНЕРЕГ: {nereg_count}\nРЕГ: {reg_count}"
        ),
        reply_markup=common_storage_sections_menu(nereg_count=nereg_count, reg_count=reg_count),
    )
    await callback.answer(f"Удалено из {_stage_title(stage)}.")


@router.callback_query(F.data.startswith("accounts:delete_common_ask:"))
async def ask_delete_common_stage(callback: CallbackQuery, config: Config) -> None:
    stage = callback.data.rsplit(":", 1)[-1]
    if stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    total = count_accounts_by_stage(config, worker_id=None, account_stage=stage)
    if not total:
        await callback.answer("В этом разделе нет аккаунтов.", show_alert=True)
        return
    await callback.message.edit_text(
        f"ВЫ УВЕРЕНЫ ЧТО ХОТИТЕ УДАЛИТЬ ВЕСЬ ОБЩИЙ {_stage_title(stage)}?\nАккаунтов: {total}\nФайлы будут удалены только с сервера.",
        reply_markup=confirm_delete_common_stage_menu(stage),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("accounts:delete_common_confirm:"))
async def confirm_delete_common_stage(callback: CallbackQuery, config: Config) -> None:
    stage = callback.data.rsplit(":", 1)[-1]
    if stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    accounts = list_accounts_by_scope(config, worker_id=None, account_stage=stage)
    if not accounts:
        await callback.answer("В этом разделе нет аккаунтов.", show_alert=True)
        return
    removed_files = _delete_local_account_files(config, accounts)
    removed_rows = delete_accounts_by_scope(config, worker_id=None, account_stage=stage)
    nereg_count, reg_count = _common_account_counts(config)
    await callback.message.edit_text(
        (
            f"Общий {_stage_title(stage)} очищен.\n"
            f"Аккаунтов удалено из бота: {removed_rows}\n"
            f"Файлов удалено с сервера: {removed_files}\n\n"
            f"Общее хранилище\nНЕРЕГ: {nereg_count}\nРЕГ: {reg_count}"
        ),
        reply_markup=common_storage_sections_menu(nereg_count=nereg_count, reg_count=reg_count),
    )
    await callback.answer("Раздел очищен.")


@router.callback_query(F.data.startswith("accounts:delete_worker_stage_ask:"))
async def ask_delete_worker_stage(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_worker_id, stage = callback.data.split(":", 3)
    if stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    worker_id = int(raw_worker_id)
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    total = count_accounts_by_stage(config, worker_id=worker_id, account_stage=stage)
    if not total:
        await callback.answer("В этом разделе нет аккаунтов.", show_alert=True)
        return
    await callback.message.edit_text(
        (
            f"ВЫ УВЕРЕНЫ ЧТО ХОТИТЕ УДАЛИТЬ ВЕСЬ {_stage_title(stage)} У ВОРКЕРА?\n"
            f"Воркер: {_worker_name(worker)}\n"
            f"Аккаунтов: {total}\n"
            "Файлы будут удалены только с сервера."
        ),
        reply_markup=confirm_delete_worker_stage_menu(worker_id, stage),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("accounts:delete_worker_stage_confirm:"))
async def confirm_delete_worker_stage(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_worker_id, stage = callback.data.split(":", 3)
    if stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    worker_id = int(raw_worker_id)
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    accounts = list_accounts_by_scope(config, worker_id=worker_id, account_stage=stage)
    if not accounts:
        await callback.answer("В этом разделе нет аккаунтов.", show_alert=True)
        return
    removed_files = _delete_local_account_files(config, accounts)
    removed_rows = delete_accounts_by_scope(config, worker_id=worker_id, account_stage=stage)
    _, nereg_count, reg_count = _worker_account_counts(config, worker)
    await callback.message.edit_text(
        (
            f"{_stage_title(stage)} воркера очищен.\n"
            f"Аккаунтов удалено из бота: {removed_rows}\n"
            f"Файлов удалено с сервера: {removed_files}\n\n"
            f"Хранилище воркера\n{_worker_name(worker)}\n\n"
            f"НЕРЕГ: {nereg_count}\n"
            f"РЕГ: {reg_count}"
        ),
        reply_markup=worker_account_sections_menu(worker_id, nereg_count=nereg_count, reg_count=reg_count),
    )
    await callback.answer("Раздел воркера очищен.")


@router.callback_query(F.data.startswith("worker:bulk_return:"))
async def bulk_return_worker_accounts_start(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_worker_id, source_stage = callback.data.split(":", 3)
    if source_stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    worker_id = int(raw_worker_id)
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    total = count_accounts_by_stage(config, worker_id=worker_id, account_stage=source_stage)
    if not total:
        await callback.answer("В этом разделе нет аккаунтов.", show_alert=True)
        return
    prefix = f"worker:bulk_return_to:{worker_id}:{source_stage}"
    cancel = f"accounts:page:worker_{source_stage}:{worker_id}:0"
    await callback.message.edit_text(
        f"Куда перенести весь {_stage_title(source_stage)} воркера {_worker_name(worker)}?\nАккаунтов: {total}",
        reply_markup=common_target_stage_menu(prefix, cancel),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("worker:bulk_return_to:"))
async def bulk_return_worker_accounts_target(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_worker_id, source_stage, target_stage = callback.data.split(":", 4)
    if source_stage not in {"nereg", "reg"} or target_stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    worker_id = int(raw_worker_id)
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    total = count_accounts_by_stage(config, worker_id=worker_id, account_stage=source_stage)
    await callback.message.edit_text(
        (
            f"ВЫ УВЕРЕНЫ ЧТО ХОТИТЕ ПЕРЕНЕСТИ ВСЕ АККИ?\n"
            f"Воркер: {_worker_name(worker)}\n"
            f"Из: {_stage_title(source_stage)}\n"
            f"В общее: {_stage_title(target_stage)}\n"
            f"Аккаунтов: {total}"
        ),
        reply_markup=confirm_bulk_return_menu(worker_id, source_stage, target_stage),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("worker:bulk_return_confirm:"))
async def bulk_return_worker_accounts_confirm(callback: CallbackQuery, config: Config) -> None:
    _, _, raw_worker_id, source_stage, target_stage = callback.data.split(":", 4)
    if source_stage not in {"nereg", "reg"} or target_stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    worker_id = int(raw_worker_id)
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    moved = move_accounts_to_common_by_scope(
        config,
        worker_id=worker_id,
        source_stage=source_stage,
        target_stage=target_stage,
    )
    _, nereg_count, reg_count = _worker_account_counts(config, worker)
    await callback.message.edit_text(
        (
            f"Перенесено в общее {_stage_title(target_stage)}: {moved}\n\n"
            f"Хранилище воркера\n{_worker_name(worker)}\n\n"
            f"НЕРЕГ: {nereg_count}\n"
            f"РЕГ: {reg_count}"
        ),
        reply_markup=worker_account_sections_menu(worker_id, nereg_count=nereg_count, reg_count=reg_count),
    )
    await callback.answer("Массовый перенос выполнен.")


@router.callback_query(F.data.startswith("accounts:bulk_assign:"))
async def bulk_assign_common_accounts_start(callback: CallbackQuery, config: Config) -> None:
    stage = callback.data.rsplit(":", 1)[-1]
    if stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    total = count_accounts_by_stage(config, worker_id=None, account_stage=stage)
    if not total:
        await callback.answer("В этом разделе нет аккаунтов.", show_alert=True)
        return
    workers = list_workers(config)
    if not workers:
        await callback.answer("Сначала добавь воркера.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Выдача аккаунтов из общего {_stage_title(stage)}\nАккаунтов доступно: {total}\n\nВыбери воркера.",
        reply_markup=bulk_assign_account_worker_keyboard(workers, stage),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("accounts:bulk_assign_worker:"))
async def bulk_assign_common_accounts_worker(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    _, _, stage, raw_worker_id = callback.data.split(":", 3)
    if stage not in {"nereg", "reg"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    worker_id = int(raw_worker_id)
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    total = count_accounts_by_stage(config, worker_id=None, account_stage=stage)
    if not total:
        await callback.answer("В этом разделе нет аккаунтов.", show_alert=True)
        return
    await state.set_state(AssignAccount.waiting_amount)
    await state.update_data(bulk_account_stage=stage, bulk_account_worker_id=worker_id)
    await callback.message.edit_text(
        (
            f"Воркер: {_worker_name(worker)}\n"
            f"Раздел: {_stage_title(stage)}\n"
            f"Доступно аккаунтов: {total}\n\n"
            "Выбери количество или отправь число."
        ),
        reply_markup=bulk_assign_account_amount_keyboard(total, stage),
    )
    await callback.answer()


async def _assign_common_accounts_amount(
    state: FSMContext,
    config: Config,
    amount: int,
) -> tuple[str, bool]:
    data = await state.get_data()
    stage = data.get("bulk_account_stage")
    worker_id = int(data.get("bulk_account_worker_id") or 0)
    if stage not in {"nereg", "reg"}:
        await state.clear()
        return "Раздел выдачи потерян. Начни заново.", False
    worker = get_worker(config, worker_id)
    if not worker:
        await state.clear()
        return "Воркер не найден.", False
    total = count_accounts_by_stage(config, worker_id=None, account_stage=stage)
    if amount <= 0:
        return "Количество должно быть больше нуля.", False
    if amount > total:
        return f"В общем {_stage_title(stage)} сейчас только {total}. Отправь число не больше {total}.", False
    assigned = assign_common_accounts_to_worker(
        config,
        worker_id=worker_id,
        source_stage=stage,
        amount=amount,
    )
    await state.clear()
    nereg_count, reg_count = _common_account_counts(config)
    return (
        f"Выдано аккаунтов: {assigned}\n"
        f"Воркер: {_worker_name(worker)}\n"
        f"Раздел: {_stage_title(stage)}\n\n"
        f"Общее хранилище\nНЕРЕГ: {nereg_count}\nРЕГ: {reg_count}",
        True,
    )


@router.callback_query(AssignAccount.waiting_amount, F.data.startswith("accounts:bulk_assign_amount:"))
async def bulk_assign_common_accounts_amount_button(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    amount = int(callback.data.rsplit(":", 1)[-1])
    text, done = await _assign_common_accounts_amount(state, config, amount)
    nereg_count, reg_count = _common_account_counts(config)
    await callback.message.edit_text(
        text,
        reply_markup=common_storage_sections_menu(nereg_count=nereg_count, reg_count=reg_count) if done else None,
    )
    await callback.answer()


@router.message(AssignAccount.waiting_amount)
async def bulk_assign_common_accounts_amount_message(message: Message, state: FSMContext, config: Config) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Отправь количество числом.")
        return
    text, done = await _assign_common_accounts_amount(state, config, int(raw))
    nereg_count, reg_count = _common_account_counts(config)
    await message.answer(
        text,
        reply_markup=common_storage_sections_menu(nereg_count=nereg_count, reg_count=reg_count) if done else None,
    )


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
    if worker_id == 0:
        prefix = f"account:return_common_to:{account_id}:{origin}:{raw_ref}:{raw_page}"
        cancel = f"account:open:{account_id}:{origin}:{raw_ref}:{raw_page}"
        await callback.message.edit_text(
            "Куда вернуть аккаунт в общем хранилище?",
            reply_markup=common_target_stage_menu(prefix, cancel),
        )
        await callback.answer()
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
    elif origin in {"common", "common_nereg", "common_reg"}:
        new_origin = "common_reg" if account.account_stage == "reg" else "common_nereg"
        new_ref_id = 0
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


@router.callback_query(F.data == "workers:menu")
async def show_workers(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    await state.clear()
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
async def add_worker_read_id(message: Message, bot: Bot, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Telegram ID должен быть числом.")
        return
    telegram_id = int(raw)
    suggested_name = await _fetch_worker_telegram_name(bot, telegram_id)
    await state.update_data(worker_telegram_id=telegram_id, worker_suggested_name=suggested_name)
    await state.set_state(CreateWorker.waiting_name)
    text = (
        f"Telegram ID: <code>{telegram_id}</code>\n\n"
        "Отправь подпись для кнопок воркера."
    )
    if suggested_name:
        text += f"\n\nTelegram имя: <b>{escape(suggested_name)}</b>\nМожно нажать кнопку ниже."
    else:
        text += "\n\nИмя из Telegram подтянуть не удалось. Если воркер еще не писал /start боту, пусть напишет, или задай подпись вручную."
    await message.answer(text, reply_markup=worker_name_choice_menu())


async def _create_worker_with_name(
    event_message: Message,
    state: FSMContext,
    config: Config,
    name: str,
) -> None:
    data = await state.get_data()
    telegram_id = int(data["worker_telegram_id"])
    worker_id = add_worker(
        config,
        name=name,
        telegram_id=telegram_id,
        department_id=None,
        created_at=utc_now_iso(),
    )
    await state.clear()
    await event_message.answer(
        f"Воркер создан\nИмя: {name}\nTelegram ID: {telegram_id}\n\nДоступ выдан к его хранилищу.",
        reply_markup=workers_menu(list_workers(config)),
    )


@router.message(CreateWorker.waiting_name)
async def add_worker_finish_manual(message: Message, state: FSMContext, config: Config) -> None:
    data = await state.get_data()
    name = _clean_worker_label(message.text or "") or data.get("worker_suggested_name")
    if not name:
        await message.answer("Отправь подпись текстом. Например: Вася или Артем.")
        return
    await _create_worker_with_name(message, state, config, str(name))


@router.callback_query(CreateWorker.waiting_name, F.data == "worker:add:name:auto")
async def add_worker_finish_auto(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    data = await state.get_data()
    name = data.get("worker_suggested_name")
    if not name:
        await callback.answer("Имя из Telegram не найдено, отправь подпись текстом.", show_alert=True)
        return
    await _create_worker_with_name(callback.message, state, config, str(name))
    await callback.answer()


@router.callback_query(F.data.startswith("worker:open:"))
async def open_worker(callback: CallbackQuery, config: Config) -> None:
    worker_id = int(callback.data.rsplit(":", 1)[-1])
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    await callback.message.edit_text(_worker_detail_text(config, worker), reply_markup=worker_detail_menu(worker_id))
    await callback.answer()


@router.callback_query(F.data.startswith("worker:rename:"))
async def rename_worker_start(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    worker_id = int(callback.data.rsplit(":", 1)[-1])
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    await state.set_state(RenameWorker.waiting_name)
    await state.update_data(rename_worker_id=worker_id)
    await callback.message.edit_text(
        f"Текущее имя: <b>{escape(worker['name'])}</b>\n\nОтправь новую подпись для кнопок."
    )
    await callback.answer()


@router.message(RenameWorker.waiting_name)
async def rename_worker_finish(message: Message, state: FSMContext, config: Config) -> None:
    name = _clean_worker_label(message.text or "")
    if not name:
        await message.answer("Подпись пустая. Отправь текстом новое имя.")
        return
    data = await state.get_data()
    worker_id = int(data["rename_worker_id"])
    worker = get_worker(config, worker_id)
    if not worker:
        await state.clear()
        await message.answer("Воркер не найден.", reply_markup=workers_menu(list_workers(config)))
        return
    update_worker_name(config, worker_id, name)
    await state.clear()
    await message.answer(
        f"Воркер переименован: <b>{escape(name)}</b>",
        reply_markup=workers_menu(list_workers(config)),
    )


@router.callback_query(F.data.startswith("worker:refresh_name:"))
async def refresh_worker_name(callback: CallbackQuery, bot: Bot, config: Config) -> None:
    worker_id = int(callback.data.rsplit(":", 1)[-1])
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    telegram_id = worker["telegram_id"]
    if not telegram_id:
        await callback.answer("У воркера нет Telegram ID.", show_alert=True)
        return
    name = await _fetch_worker_telegram_name(bot, int(telegram_id))
    if not name:
        await callback.answer(
            "Не удалось подтянуть имя. Пусть воркер напишет /start боту или задай имя вручную.",
            show_alert=True,
        )
        return
    update_worker_name(config, worker_id, name)
    worker = get_worker(config, worker_id)
    await callback.message.edit_text(_worker_detail_text(config, worker), reply_markup=worker_detail_menu(worker_id))
    await callback.answer("Имя обновлено из Telegram.")


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
async def show_proxies(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    await state.clear()
    rows = list_available_proxies(config)
    total = count_available_proxies(config)
    text = f"Прокси\n\nСвободно: {total}"
    if rows:
        if total > len(rows):
            text += f"\nПоказаны первые {len(rows)}."
    else:
        text += "\n\nПрокси пока нет."
    await callback.message.edit_text(text, reply_markup=proxies_storage_keyboard(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("proxy:open:"))
async def open_proxy(callback: CallbackQuery, config: Config) -> None:
    proxy_id = int(callback.data.rsplit(":", 1)[-1])
    proxy = next((row for row in list_available_proxies(config, limit=100000) if row["id"] == proxy_id), None)
    if not proxy:
        await callback.answer("Прокси уже выдан или не найден.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Прокси #{proxy['id']}\n\n<b>{escape(proxy['proxy'])}</b>",
        reply_markup=proxy_detail_keyboard(proxy_id),
    )
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
    invalid = [line for line in lines if len(line.split(":")) < 4]
    if invalid:
        await message.answer(
            "Есть строки не в формате host:port:user:pass.\n\n"
            f"Первая ошибка: <code>{escape(invalid[0])}</code>"
        )
        return
    now = utc_now_iso()
    ids = [add_proxy(config, line, now) for line in lines]
    await state.clear()
    await message.answer(
        f"Добавлено прокси: {len(ids)}\nСвободно всего: {count_available_proxies(config)}",
        reply_markup=proxies_storage_keyboard(list_available_proxies(config)),
    )


@router.callback_query(F.data == "proxies:assign")
async def assign_proxy_start(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    available = count_available_proxies(config)
    if not available:
        await callback.answer("Свободных прокси нет.", show_alert=True)
        return
    workers = list_workers(config)
    if not workers:
        await callback.answer("Сначала добавь воркера.", show_alert=True)
        return
    await state.set_state(AssignProxy.waiting_worker_id)
    await callback.message.edit_text(
        f"Свободных прокси: {available}\n\nВыбери воркера, кому выдать прокси.",
        reply_markup=proxy_worker_select_keyboard(workers),
    )
    await callback.answer()


@router.callback_query(AssignProxy.waiting_worker_id, F.data.startswith("proxies:assign_worker:"))
async def assign_proxy_worker_selected(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    worker_id = int(callback.data.rsplit(":", 1)[-1])
    worker = get_worker(config, worker_id)
    if not worker:
        await callback.answer("Воркер не найден.", show_alert=True)
        return
    available = count_available_proxies(config)
    if not available:
        await state.clear()
        await callback.answer("Свободных прокси нет.", show_alert=True)
        await callback.message.edit_text("Свободных прокси нет.", reply_markup=proxies_menu())
        return
    await state.update_data(proxy_worker_id=worker_id)
    await state.set_state(AssignProxy.waiting_amount)
    await callback.message.edit_text(
        f"Воркер: {_worker_name(worker)}\nСвободных прокси: {available}\n\nВыбери количество или отправь число.",
        reply_markup=proxy_amount_keyboard(available),
    )
    await callback.answer()


async def _assign_proxy_amount(
    state: FSMContext,
    config: Config,
    amount: int,
) -> tuple[str, bool]:
    available = count_available_proxies(config)
    if amount <= 0:
        return "Количество должно быть больше нуля.", False
    if amount > available:
        return f"Свободно только {available}. Отправь число не больше {available}.", False
    data = await state.get_data()
    worker_id = int(data["proxy_worker_id"])
    worker = get_worker(config, worker_id)
    if not worker:
        await state.clear()
        return "Воркер не найден.", False
    assigned = assign_available_proxies_to_worker(config, worker_id, amount)
    await state.clear()
    return (
        f"Выдано прокси: {assigned}\n"
        f"Воркер: {_worker_name(worker)}\n"
        f"Свободно осталось: {count_available_proxies(config)}",
        True,
    )


@router.callback_query(AssignProxy.waiting_amount, F.data.startswith("proxies:assign_amount:"))
async def assign_proxy_amount_button(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    amount = int(callback.data.rsplit(":", 1)[-1])
    text, done = await _assign_proxy_amount(state, config, amount)
    await callback.message.edit_text(
        text,
        reply_markup=proxies_storage_keyboard(list_available_proxies(config)) if done else None,
    )
    await callback.answer()


@router.message(AssignProxy.waiting_amount)
async def assign_proxy_finish(message: Message, state: FSMContext, config: Config) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Отправь количество числом.")
        return
    text, done = await _assign_proxy_amount(state, config, int(raw))
    await message.answer(
        text,
        reply_markup=proxies_storage_keyboard(list_available_proxies(config)) if done else None,
    )


@router.callback_query(F.data == "worker:self_proxy:menu")
async def show_worker_proxy_menu(callback: CallbackQuery, config: Config, current_worker) -> None:
    remaining = count_worker_proxies(config, current_worker["id"], "assigned")
    total = count_worker_proxies(config, current_worker["id"])
    await callback.message.edit_text(
        f"Прокси\n\nДоступно: {remaining}/{total}",
        reply_markup=worker_proxy_menu(remaining=remaining, total=total),
    )
    await callback.answer()


@router.callback_query(F.data == "worker:self_proxy:get")
async def worker_get_proxy(callback: CallbackQuery, config: Config, current_worker) -> None:
    worker_id = current_worker["id"]
    before_total = count_worker_proxies(config, worker_id)
    proxy = pop_worker_proxy(config, worker_id)
    if not proxy:
        remaining = count_worker_proxies(config, worker_id, "assigned")
        total = count_worker_proxies(config, worker_id)
        await callback.message.edit_text(
            f"Прокси\n\nДоступно: {remaining}/{total}",
            reply_markup=worker_proxy_menu(remaining=remaining, total=total),
        )
        await callback.answer("Прокси для тебя нет.", show_alert=True)
        return
    remaining = count_worker_proxies(config, worker_id, "assigned")
    total = before_total
    await callback.message.answer(
        f"Прокси:\n<b>{escape(proxy['proxy'])}</b>\n\nОсталось: {remaining}/{total}"
    )
    await callback.message.edit_text(
        f"Прокси\n\nДоступно: {remaining}/{total}",
        reply_markup=worker_proxy_menu(remaining=remaining, total=total),
    )
    await callback.answer("Прокси выдан.")


@router.callback_query(F.data == "settings:menu")
async def show_settings(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Настройки\n\nКонфиг загружается из .env.",
        reply_markup=placeholder_menu("settings"),
    )
    await callback.answer()
