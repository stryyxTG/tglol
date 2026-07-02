from __future__ import annotations

from collections.abc import Sequence
from math import ceil

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


ACCOUNTS_PER_PAGE = 14


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Аккаунты", callback_data="accounts:menu")
    builder.button(text="Прокси", callback_data="proxies:menu")
    builder.button(text="Воркеры", callback_data="workers:menu")
    builder.button(text="Отделы", callback_data="departments:menu")
    builder.button(text="Настройки", callback_data="settings:menu")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def accounts_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Добавить аккаунт", callback_data="accounts:add")
    builder.button(text="Общее хранилище", callback_data="accounts:page:common:0:0")
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


def _account_label(account) -> str:
    name = account.phone or account.username or account.telegram_user_id or "без данных"
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
    if page > 0:
        builder.button(text="‹ Назад", callback_data=f"accounts:page:{origin}:{ref_id}:{prev_page}")
    builder.button(text=f"{page + 1}/{pages}", callback_data="noop")
    if page + 1 < pages:
        builder.button(text="Вперед ›", callback_data=f"accounts:page:{origin}:{ref_id}:{next_page}")

    if origin == "worker":
        builder.button(text="К воркерам", callback_data="accounts:worker_storage")
    elif origin in {"worker_nereg", "worker_reg"}:
        builder.button(text="К разделам воркера", callback_data=f"worker:account_sections:{ref_id}")
    builder.button(text="Меню аккаунтов", callback_data="accounts:menu")
    builder.adjust(*([1] * len(accounts)), 3, 1, 1)
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
    builder.button(text="Скачать session", callback_data=f"accounts:file:session:{account_id}")
    builder.button(text="Скачать JSON", callback_data=f"accounts:file:json:{account_id}")
    builder.button(text="Выдать воркеру", callback_data=f"account:assign:{account_id}:{origin}:{ref_id}:{page}")
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
    builder.button(text="Назад", callback_data=back)
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
    next_step = "ask2" if step == 1 else "confirm"
    label = "ДА, ПРОДОЛЖИТЬ" if step == 1 else "ДА, ПОДТВЕРЖДАЮ"
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


def departments_menu(departments: Sequence) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for department in departments:
        builder.button(text=department["name"], callback_data=f"department:open:{department['id']}")
    builder.button(text="Добавить отдел", callback_data="departments:add")
    builder.button(text="Назад", callback_data="main:menu")
    builder.adjust(*([1] * len(departments)), 1, 1)
    return builder.as_markup()


def department_detail_menu(department_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Удалить отдел", callback_data=f"department:delete:ask:{department_id}")
    builder.button(text="Назад", callback_data="departments:menu")
    builder.adjust(1)
    return builder.as_markup()


def confirm_delete_department_menu(department_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, удалить", callback_data=f"department:delete:confirm:{department_id}")
    builder.button(text="Отмена", callback_data=f"department:open:{department_id}")
    builder.adjust(1)
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
    builder.button(text="Удалить воркера", callback_data=f"worker:delete:ask:{worker_id}")
    builder.button(text="Назад", callback_data="workers:menu")
    builder.adjust(1)
    return builder.as_markup()


def worker_account_sections_menu(worker_id: int, *, nereg_count: int, reg_count: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"НЕРЕГ | {nereg_count}", callback_data=f"accounts:page:worker_nereg:{worker_id}:0")
    builder.button(text=f"РЕГ | {reg_count}", callback_data=f"accounts:page:worker_reg:{worker_id}:0")
    builder.button(text="Назад к воркерам", callback_data="accounts:worker_storage")
    builder.adjust(1)
    return builder.as_markup()


def confirm_delete_worker_menu(worker_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, удалить", callback_data=f"worker:delete:confirm:{worker_id}")
    builder.button(text="Отмена", callback_data=f"worker:open:{worker_id}")
    builder.adjust(1)
    return builder.as_markup()


def worker_department_select_menu(departments: Sequence) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for department in departments:
        builder.button(text=department["name"], callback_data=f"worker:add:dept:{department['id']}")
    builder.button(text="Без отдела", callback_data="worker:add:dept:0")
    builder.button(text="Отмена", callback_data="workers:menu")
    builder.adjust(*([1] * len(departments)), 1, 1)
    return builder.as_markup()


def proxies_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Добавить прокси", callback_data="proxies:add")
    builder.button(text="Назад", callback_data="main:menu")
    builder.adjust(1)
    return builder.as_markup()


def worker_self_menu(*, department_name: str | None, nereg_count: int, reg_count: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"НЕРЕГ | {nereg_count}", callback_data="worker:self:page:nereg:0")
    builder.button(text=f"РЕГ | {reg_count}", callback_data="worker:self:page:reg:0")
    builder.button(text="Обновить", callback_data="worker:self:menu")
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
    if page > 0:
        builder.button(text="‹ Назад", callback_data=f"worker:self:page:{stage}:{page - 1}")
    builder.button(text=f"{page + 1}/{pages}", callback_data="noop")
    if page + 1 < pages:
        builder.button(text="Вперед ›", callback_data=f"worker:self:page:{stage}:{page + 1}")
    builder.button(text="Мое хранилище", callback_data="worker:self:menu")
    builder.adjust(*([1] * len(accounts)), 3, 1)
    return builder.as_markup()


def worker_self_account_detail_menu(
    account_id: int,
    *,
    stage: str,
    page: int,
    account_stage: str = "nereg",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
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
    next_step = "ask2" if step == 1 else "confirm"
    label = "ДА, ПРОДОЛЖИТЬ" if step == 1 else "ДА, ПОДТВЕРЖДАЮ"
    builder.button(
        text=label,
        callback_data=f"worker:self_stage_{next_step}:{account_id}:{target_stage}:{origin_stage}:{page}",
    )
    builder.button(text="Отмена", callback_data=f"worker:self_account:{account_id}:{origin_stage}:{page}")
    builder.adjust(1)
    return builder.as_markup()
