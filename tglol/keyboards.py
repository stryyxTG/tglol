from __future__ import annotations

import re
from collections.abc import Sequence
from math import ceil

from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


ACCOUNTS_PER_PAGE = 14


def admin_reply_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Админ панель")]],
        resize_keyboard=True,
        input_field_placeholder="Админ панель",
    )


def worker_reply_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Рабочая панель")]],
        resize_keyboard=True,
        input_field_placeholder="Рабочая панель",
    )


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Аккаунты", callback_data="accounts:menu")
    builder.button(text="Прокси", callback_data="proxies:menu")
    builder.button(text="Воркеры", callback_data="workers:menu")
    builder.button(text="Настройки", callback_data="settings:menu")
    builder.adjust(2, 2)
    return builder.as_markup()


def accounts_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Добавить аккаунт", callback_data="accounts:add")
    builder.button(text="Общее хранилище", callback_data="accounts:common_sections")
    builder.button(text="Хранилища воркеров", callback_data="accounts:worker_storage")
    builder.button(text="Назад", callback_data="main:menu")
    builder.adjust(1)
    return builder.as_markup()


def add_account_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="По номеру + код", callback_data="accounts:add:code")
    builder.button(text=".session", callback_data="accounts:add:session")
    builder.button(text=".session + .json", callback_data="accounts:add:session_json")
    builder.button(text="Массово .zip", callback_data="accounts:add:zip")
    builder.button(text="Назад", callback_data="accounts:add")
    builder.adjust(1)
    return builder.as_markup()


def add_account_target_menu(workers: Sequence) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Основное хранилище", callback_data="accounts:add_target:common")
    for worker in workers:
        builder.button(
            text=worker["name"],
            callback_data=f"accounts:add_target:worker:{worker['id']}",
        )
    builder.button(text="Назад", callback_data="accounts:menu")
    builder.adjust(1)
    return builder.as_markup()


def digit_code_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for digit in "123456789":
        builder.button(text=digit, callback_data=f"code:digit:{digit}")
    builder.button(text="Очистить", callback_data="code:clear")
    builder.button(text="0", callback_data="code:digit:0")
    builder.button(text="Стереть", callback_data="code:backspace")
    builder.button(text="Подтвердить", callback_data="code:done")
    builder.button(text="Отмена", callback_data="accounts:add")
    builder.adjust(3, 3, 3, 3, 1, 1)
    return builder.as_markup()


def placeholder_menu(section: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад", callback_data="main:menu")
    builder.button(text="Обновить", callback_data=f"{section}:menu")
    builder.adjust(1)
    return builder.as_markup()


def skip_json_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Импорт без JSON", callback_data="accounts:add:session:no_json")
    builder.button(text="Отмена", callback_data="accounts:add")
    builder.adjust(1)
    return builder.as_markup()


def _format_phone(value) -> str | None:
    if value in (None, ""):
        return None
    phone = str(value).strip()
    digits = re.sub(r"\D+", "", phone)
    if len(digits) == 11 and digits.startswith("7"):
        return f"{digits[0]} {digits[1:4]} {digits[4:7]} {digits[7:]}"
    return phone


def _account_label(account) -> str:
    name = _format_phone(account.phone) or account.username or account.telegram_user_id or "без данных"
    stage = "РЕГ" if account.account_stage == "reg" else "НЕРЕГ"
    return f"#{account.id} | {name} | {stage} | {account.status}"


def accounts_page_keyboard(
    accounts: Sequence,
    *,
    total: int,
    page: int,
    origin: str,
    ref_id: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for account in accounts:
        builder.button(
            text=_account_label(account),
            callback_data=f"account:open:{account.id}:{origin}:{ref_id}:{page}",
        )

    pages = max(1, ceil(total / ACCOUNTS_PER_PAGE))
    prev_page = max(0, page - 1)
    next_page = min(pages - 1, page + 1)
    nav_count = 0
    if page > 0:
        builder.button(text="‹ Назад", callback_data=f"accounts:page:{origin}:{ref_id}:{prev_page}")
        nav_count += 1
    builder.button(text=f"{page + 1}/{pages}", callback_data="noop")
    nav_count += 1
    if page + 1 < pages:
        builder.button(text="Вперед ›", callback_data=f"accounts:page:{origin}:{ref_id}:{next_page}")
        nav_count += 1

    action_count = 0
    if origin in {"common_nereg", "common_reg"} and total > 0:
        stage = "nereg" if origin == "common_nereg" else "reg"
        builder.button(text="Выдать воркеру", callback_data=f"accounts:bulk_assign:{stage}")
        builder.button(text="Скачать ZIP", callback_data=f"accounts:zip_common:{stage}")
        builder.button(text="Удалить весь раздел", callback_data=f"accounts:delete_common_ask:{stage}")
        action_count += 3

    if origin == "worker":
        builder.button(text="К воркерам", callback_data="accounts:worker_storage")
        action_count += 1
    elif origin in {"worker_nereg", "worker_reg"}:
        stage = "nereg" if origin == "worker_nereg" else "reg"
        if total > 0:
            builder.button(text="Массово вернуть в общее", callback_data=f"worker:bulk_return:{ref_id}:{stage}")
            builder.button(text="Удалить весь раздел", callback_data=f"accounts:delete_worker_stage_ask:{ref_id}:{stage}")
            action_count += 2
        builder.button(text="К разделам воркера", callback_data=f"worker:account_sections:{ref_id}")
        action_count += 1
    elif origin in {"common_nereg", "common_reg"}:
        builder.button(text="К разделам общего", callback_data="accounts:common_sections")
        action_count += 1
    builder.button(text="Меню аккаунтов", callback_data="accounts:menu")
    action_count += 1
    builder.adjust(*([1] * len(accounts)), nav_count, *([1] * action_count))
    return builder.as_markup()


def account_detail_menu(
    account_id: int,
    *,
    account_stage: str = "nereg",
    origin: str,
    ref_id: int,
    page: int,
) -> InlineKeyboardMarkup:
    back = f"accounts:page:{origin}:{ref_id}:{page}"
    builder = InlineKeyboardBuilder()
    builder.button(text="Скопировать номер", callback_data=f"accounts:phone:{account_id}")
    builder.button(text="Получить код из Verification Codes", callback_data=f"account:code:{account_id}")
    builder.button(text="Скачать session", callback_data=f"accounts:file:session:{account_id}")
    builder.button(text="Скачать JSON", callback_data=f"accounts:file:json:{account_id}")
    builder.button(text="Выдать воркеру", callback_data=f"account:assign:{account_id}:{origin}:{ref_id}:{page}")
    if origin in {"worker", "worker_nereg", "worker_reg"}:
        builder.button(text="Вернуть в общее", callback_data=f"account:return_common:{account_id}:{origin}:{ref_id}:{page}")
    if account_stage == "reg":
        builder.button(
            text="Перенести в НЕРЕГ",
            callback_data=f"account:stage_ask1:{account_id}:nereg:{origin}:{ref_id}:{page}",
        )
    else:
        builder.button(
            text="Пометить зареганным",
            callback_data=f"account:stage_ask1:{account_id}:reg:{origin}:{ref_id}:{page}",
        )
    if origin in {"common", "common_nereg", "common_reg", "worker", "worker_nereg", "worker_reg"}:
        builder.button(text="Удалить аккаунт", callback_data=f"account:delete_ask:{account_id}:{origin}:{ref_id}:{page}")
    builder.button(text="Назад", callback_data=back)
    builder.adjust(1)
    return builder.as_markup()


def common_storage_sections_menu(*, nereg_count: int, reg_count: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"НЕРЕГ | {nereg_count}", callback_data="accounts:page:common_nereg:0:0")
    builder.button(text=f"РЕГ | {reg_count}", callback_data="accounts:page:common_reg:0:0")
    builder.button(text="Назад", callback_data="accounts:menu")
    builder.adjust(1)
    return builder.as_markup()


def common_target_stage_menu(callback_prefix: str, cancel_callback: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="В общий НЕРЕГ", callback_data=f"{callback_prefix}:nereg")
    builder.button(text="В общий РЕГ", callback_data=f"{callback_prefix}:reg")
    builder.button(text="Отмена", callback_data=cancel_callback)
    builder.adjust(1)
    return builder.as_markup()


def download_zip_amount_keyboard(total: int, stage: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for amount in (1, 5, 10, 20, 50):
        if amount <= total:
            builder.button(text=str(amount), callback_data=f"accounts:zip_amount:{stage}:{amount}")
    builder.button(text=f"Все {total}", callback_data=f"accounts:zip_amount:{stage}:all")
    builder.button(text="Отмена", callback_data=f"accounts:page:common_{stage}:0:0")
    builder.adjust(3, 2, 1, 1)
    return builder.as_markup()


def confirm_delete_account_menu(account_id: int, origin: str, ref_id: int, page: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="ДА, УДАЛИТЬ", callback_data=f"account:delete_confirm:{account_id}:{origin}:{ref_id}:{page}")
    builder.button(text="Отмена", callback_data=f"account:open:{account_id}:{origin}:{ref_id}:{page}")
    builder.adjust(1)
    return builder.as_markup()


def confirm_delete_common_stage_menu(stage: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="ДА, УДАЛИТЬ ВСЕ", callback_data=f"accounts:delete_common_confirm:{stage}")
    builder.button(text="Отмена", callback_data=f"accounts:page:common_{stage}:0:0")
    builder.adjust(1)
    return builder.as_markup()


def confirm_delete_worker_stage_menu(worker_id: int, stage: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="ДА, УДАЛИТЬ ВСЕ", callback_data=f"accounts:delete_worker_stage_confirm:{worker_id}:{stage}")
    builder.button(text="Отмена", callback_data=f"accounts:page:worker_{stage}:{worker_id}:0")
    builder.adjust(1)
    return builder.as_markup()


def confirm_bulk_return_menu(worker_id: int, source_stage: str, target_stage: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="ДА, ПЕРЕНЕСТИ ВСЕ",
        callback_data=f"worker:bulk_return_confirm:{worker_id}:{source_stage}:{target_stage}",
    )
    builder.button(text="Отмена", callback_data=f"accounts:page:worker_{source_stage}:{worker_id}:0")
    builder.adjust(1)
    return builder.as_markup()


def confirm_account_stage_menu(
    account_id: int,
    target_stage: str,
    origin: str,
    ref_id: int,
    page: int,
    step: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    next_step = "confirm"
    label = "ДА"
    builder.button(
        text=label,
        callback_data=f"account:stage_{next_step}:{account_id}:{target_stage}:{origin}:{ref_id}:{page}",
    )
    builder.button(text="Отмена", callback_data=f"account:open:{account_id}:{origin}:{ref_id}:{page}")
    builder.adjust(1)
    return builder.as_markup()


def assign_account_keyboard(
    workers: Sequence,
    *,
    account_id: int,
    origin: str,
    ref_id: int,
    page: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for worker in workers:
        label = worker["name"]
        builder.button(
            text=label,
            callback_data=f"account:assign_to:{account_id}:{worker['id']}:{origin}:{ref_id}:{page}",
        )
    builder.button(
        text="Вернуть в общее",
        callback_data=f"account:assign_to:{account_id}:0:{origin}:{ref_id}:{page}",
    )
    builder.button(text="Назад", callback_data=f"account:open:{account_id}:{origin}:{ref_id}:{page}")
    builder.adjust(*([1] * len(workers)), 1, 1)
    return builder.as_markup()


def bulk_assign_account_worker_keyboard(workers: Sequence, stage: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for worker in workers:
        builder.button(text=worker["name"], callback_data=f"accounts:bulk_assign_worker:{stage}:{worker['id']}")
    builder.button(text="Отмена", callback_data=f"accounts:page:common_{stage}:0:0")
    builder.adjust(*([1] * len(workers)), 1)
    return builder.as_markup()


def bulk_assign_account_amount_keyboard(available: int, stage: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for amount in (1, 2, 3, 5, 10):
        if amount <= available:
            builder.button(text=str(amount), callback_data=f"accounts:bulk_assign_amount:{amount}")
    builder.button(text=f"Все {available}", callback_data=f"accounts:bulk_assign_amount:{available}")
    builder.button(text="Отмена", callback_data=f"accounts:page:common_{stage}:0:0")
    builder.adjust(3, 2, 1, 1)
    return builder.as_markup()


def worker_storage_keyboard(workers: Sequence) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for worker in workers:
        builder.button(
            text=worker["name"],
            callback_data=f"worker:account_sections:{worker['id']}",
        )
    builder.button(text="Назад", callback_data="accounts:menu")
    builder.adjust(*([1] * len(workers)), 1)
    return builder.as_markup()


def workers_menu(workers: Sequence) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for worker in workers:
        builder.button(text=worker["name"], callback_data=f"worker:open:{worker['id']}")
    builder.button(text="Добавить воркера", callback_data="workers:add")
    builder.button(text="Назад", callback_data="main:menu")
    builder.adjust(*([1] * len(workers)), 1, 1)
    return builder.as_markup()


def worker_detail_menu(worker_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Аккаунты воркера", callback_data=f"worker:account_sections:{worker_id}")
    builder.button(text="Задать имя вручную", callback_data=f"worker:rename:{worker_id}")
    builder.button(text="Обновить имя из Telegram", callback_data=f"worker:refresh_name:{worker_id}")
    builder.button(text="Удалить воркера", callback_data=f"worker:delete:ask:{worker_id}")
    builder.button(text="Назад", callback_data="workers:menu")
    builder.adjust(1)
    return builder.as_markup()


def worker_name_choice_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Взять имя из Telegram", callback_data="worker:add:name:auto")
    builder.button(text="Отмена", callback_data="workers:menu")
    builder.adjust(1)
    return builder.as_markup()


def worker_account_sections_menu(worker_id: int, *, nereg_count: int, reg_count: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"НЕРЕГ | {nereg_count}", callback_data=f"accounts:page:worker_nereg:{worker_id}:0")
    builder.button(text=f"РЕГ | {reg_count}", callback_data=f"accounts:page:worker_reg:{worker_id}:0")
    if nereg_count:
        builder.button(text="Весь НЕРЕГ в общее", callback_data=f"worker:bulk_return:{worker_id}:nereg")
    if reg_count:
        builder.button(text="Весь РЕГ в общее", callback_data=f"worker:bulk_return:{worker_id}:reg")
    builder.button(text="Назад к воркерам", callback_data="accounts:worker_storage")
    builder.adjust(1)
    return builder.as_markup()


def confirm_delete_worker_menu(worker_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, удалить", callback_data=f"worker:delete:confirm:{worker_id}")
    builder.button(text="Отмена", callback_data=f"worker:open:{worker_id}")
    builder.adjust(1)
    return builder.as_markup()


def proxies_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Добавить прокси", callback_data="proxies:add")
    builder.button(text="Выдать воркеру", callback_data="proxies:assign")
    builder.button(text="Назад", callback_data="main:menu")
    builder.adjust(1)
    return builder.as_markup()


def proxies_storage_keyboard(proxies: Sequence) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for proxy in proxies:
        label = str(proxy["proxy"])
        if len(label) > 48:
            label = f"{label[:45]}..."
        builder.button(text=f"#{proxy['id']} | {label}", callback_data=f"proxy:open:{proxy['id']}")
    builder.button(text="Добавить прокси", callback_data="proxies:add")
    builder.button(text="Выдать воркеру", callback_data="proxies:assign")
    builder.button(text="Назад", callback_data="main:menu")
    builder.adjust(*([1] * len(proxies)), 1, 1, 1)
    return builder.as_markup()


def proxy_detail_keyboard(proxy_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Выдать воркеру", callback_data="proxies:assign")
    builder.button(text="Назад к прокси", callback_data="proxies:menu")
    builder.adjust(1)
    return builder.as_markup()


def proxy_worker_select_keyboard(workers: Sequence) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for worker in workers:
        builder.button(text=worker["name"], callback_data=f"proxies:assign_worker:{worker['id']}")
    builder.button(text="Отмена", callback_data="proxies:menu")
    builder.adjust(*([1] * len(workers)), 1)
    return builder.as_markup()


def proxy_amount_keyboard(available: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for amount in (1, 2, 3, 5, 10):
        if amount <= available:
            builder.button(text=str(amount), callback_data=f"proxies:assign_amount:{amount}")
    builder.button(text=f"Все {available}", callback_data=f"proxies:assign_amount:{available}")
    builder.button(text="Отмена", callback_data="proxies:menu")
    builder.adjust(3, 2, 1, 1)
    return builder.as_markup()


def worker_proxy_menu(*, remaining: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Получить прокси | {remaining}/{total}", callback_data="worker:self_proxy:get")
    builder.button(text="Обновить", callback_data="worker:self_proxy:menu")
    builder.button(text="Мое хранилище", callback_data="worker:self:menu")
    builder.adjust(1)
    return builder.as_markup()


def worker_self_menu(*, nereg_count: int, reg_count: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"❌ НЕРЕГ| {nereg_count}", callback_data="worker:self:page:nereg:0")
    builder.button(text=f"✅ РЕГ | {reg_count}", callback_data="worker:self:page:reg:0")
    builder.button(text="🌐ПРОКСИ", callback_data="worker:self_proxy:menu")
    builder.button(text="🆕ОБНОВИТЬ", callback_data="worker:self:menu")
    builder.adjust(1)
    return builder.as_markup()


def worker_self_accounts_page_keyboard(
    accounts: Sequence,
    *,
    total: int,
    page: int,
    stage: str,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for account in accounts:
        builder.button(
            text=_account_label(account),
            callback_data=f"worker:self_account:{account.id}:{stage}:{page}",
        )

    pages = max(1, ceil(total / ACCOUNTS_PER_PAGE))
    nav_count = 0
    if page > 0:
        builder.button(text="‹ Назад", callback_data=f"worker:self:page:{stage}:{page - 1}")
        nav_count += 1
    builder.button(text=f"{page + 1}/{pages}", callback_data="noop")
    nav_count += 1
    if page + 1 < pages:
        builder.button(text="Вперед ›", callback_data=f"worker:self:page:{stage}:{page + 1}")
        nav_count += 1
    builder.button(text="Мое хранилище", callback_data="worker:self:menu")
    builder.adjust(*([1] * len(accounts)), nav_count, 1)
    return builder.as_markup()


def worker_self_account_detail_menu(
    account_id: int,
    *,
    stage: str,
    page: int,
    account_stage: str = "nereg",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Скопировать номер", callback_data=f"worker:self_phone:{account_id}")
    builder.button(text="Получить код из Verification Codes", callback_data=f"worker:self_code:{account_id}")
    if account_stage == "reg":
        builder.button(
            text="Перенести в НЕРЕГ",
            callback_data=f"worker:self_stage_ask1:{account_id}:nereg:{stage}:{page}",
        )
    else:
        builder.button(
            text="Пометить зареганным",
            callback_data=f"worker:self_stage_ask1:{account_id}:reg:{stage}:{page}",
        )
    builder.button(text="Назад", callback_data=f"worker:self:page:{stage}:{page}")
    builder.adjust(1)
    return builder.as_markup()


def confirm_worker_account_stage_menu(
    account_id: int,
    target_stage: str,
    origin_stage: str,
    page: int,
    step: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    next_step = "confirm"
    label = "ДА"
    builder.button(
        text=label,
        callback_data=f"worker:self_stage_{next_step}:{account_id}:{target_stage}:{origin_stage}:{page}",
    )
    builder.button(text="Отмена", callback_data=f"worker:self_account:{account_id}:{origin_stage}:{page}")
    builder.adjust(1)
    return builder.as_markup()
