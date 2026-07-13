from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
from pathlib import Path
import re
from time import monotonic
from typing import Any

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.updates import GetStateRequest
from telethon.tl.types import User


CODE_RE = re.compile(r"(?<!\d)(\d[\d\s-]{2,14}\d)(?!\d)")
CODE_CONTEXT_RE = re.compile(r"\b(code|verification|verify|otp|passcode|парол|код|подтверж)\b", re.IGNORECASE)
VERIFICATION_CODE_PEERS = ("VerificationCodes", "@VerificationCodes")
logger = logging.getLogger(__name__)

ACCOUNT_ACTIVITY_TTL = 10 * 60
_active_clients: dict[str, TelegramClient] = {}
_active_until: dict[str, float] = {}
_active_cleanup_tasks: dict[str, asyncio.Task[None]] = {}
_active_locks: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True)
class CodeRequest:
    phone_code_hash: str | None
    delivery_type: str
    next_type: str | None
    timeout: int | None
    code_length: int | None
    already_authorized: bool = False
    user: User | None = None


def client_for(
    session_path: Path,
    api_id: int,
    api_hash: str,
    runtime: dict[str, str],
) -> TelegramClient:
    return TelegramClient(
        str(session_path),
        api_id,
        api_hash,
        device_model=runtime.get("device") or "Desktop",
        system_version=runtime.get("sdk") or "Windows 11 x64",
        app_version=runtime.get("app_version") or "6.9.3 x64",
        lang_code=runtime.get("lang_code") or "en",
        system_lang_code=runtime.get("system_lang_code") or "en-US",
        connection_retries=2,
        request_retries=2,
        retry_delay=1,
        timeout=10,
    )


def _session_key(session_path: Path) -> str:
    return str(session_path.resolve()).casefold()


async def _disconnect_after_activity(key: str, client: TelegramClient) -> None:
    try:
        while _active_clients.get(key) is client:
            delay = _active_until.get(key, 0.0) - monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
                continue

            _active_clients.pop(key, None)
            _active_until.pop(key, None)
            with suppress(Exception):
                await client(UpdateStatusRequest(offline=True))
            if client.is_connected():
                await client.disconnect()
            return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.info('Cannot close active Telegram session %s: %s', key, exc)
    finally:
        if _active_cleanup_tasks.get(key) is asyncio.current_task():
            _active_cleanup_tasks.pop(key, None)


async def _get_active_client(
    session_path: Path,
    api_id: int,
    api_hash: str,
    runtime: dict[str, str],
) -> TelegramClient:
    key = _session_key(session_path)
    lock = _active_locks.setdefault(key, asyncio.Lock())
    async with lock:
        client = _active_clients.get(key)
        cleanup_task = _active_cleanup_tasks.get(key)
        if client is None or not client.is_connected():
            if cleanup_task is not None and not cleanup_task.done():
                cleanup_task.cancel()
            if client is not None:
                with suppress(Exception):
                    await client.disconnect()
            client = client_for(session_path, api_id, api_hash, runtime)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise RuntimeError('session is not authorized')
            _active_clients[key] = client
            cleanup_task = None

        _active_until[key] = monotonic() + ACCOUNT_ACTIVITY_TTL
        if cleanup_task is None or cleanup_task.done():
            _active_cleanup_tasks[key] = asyncio.create_task(_disconnect_after_activity(key, client))
        return client


async def activate_account_session(
    session_path: Path,
    api_id: int,
    api_hash: str,
    runtime: dict[str, str],
) -> User:
    '''Wake an authorized account and keep its MTProto connection alive temporarily.'''
    client = await _get_active_client(session_path, api_id, api_hash, runtime)
    await client(UpdateStatusRequest(offline=False))
    await client(GetStateRequest())
    await client.get_dialogs(limit=1)
    me = await client.get_me()
    if me is None:
        raise RuntimeError('authorized session has no account data')
    return me


async def close_active_sessions() -> None:
    tasks = list(_active_cleanup_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    clients = list(_active_clients.values())
    _active_clients.clear()
    _active_until.clear()
    _active_cleanup_tasks.clear()
    _active_locks.clear()
    for client in clients:
        with suppress(Exception):
            if client.is_connected():
                await client(UpdateStatusRequest(offline=True))
                await client.disconnect()


async def send_code(
    session_path: Path,
    phone: str,
    api_id: int,
    api_hash: str,
    runtime: dict[str, str],
) -> CodeRequest:
    client = client_for(session_path, api_id, api_hash, runtime)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            logger.info("Telegram session is already authorized: phone=%s", phone)
            return CodeRequest(
                phone_code_hash=None,
                delivery_type="Authorized",
                next_type=None,
                timeout=None,
                code_length=None,
                already_authorized=True,
                user=me,
            )

        sent = await client.send_code_request(phone)
        logger.info(
            "Telegram login code requested: phone=%s delivery=%s next=%s timeout=%s length=%s",
            phone,
            type(sent.type).__name__,
            type(sent.next_type).__name__ if sent.next_type else None,
            sent.timeout,
            getattr(sent.type, "length", None),
        )
        return CodeRequest(
            phone_code_hash=sent.phone_code_hash,
            delivery_type=type(sent.type).__name__,
            next_type=type(sent.next_type).__name__ if sent.next_type else None,
            timeout=sent.timeout,
            code_length=getattr(sent.type, "length", None),
        )
    finally:
        await client.disconnect()


async def sign_in_code(
    session_path: Path,
    phone: str,
    code: str,
    phone_code_hash: str,
    api_id: int,
    api_hash: str,
    runtime: dict[str, str],
) -> User:
    client = client_for(session_path, api_id, api_hash, runtime)
    await client.connect()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        me = await client.get_me()
        if me is None:
            raise RuntimeError("Login succeeded but account info is empty")
        return me
    finally:
        await client.disconnect()


async def sign_in_password(
    session_path: Path,
    password: str,
    api_id: int,
    api_hash: str,
    runtime: dict[str, str],
) -> User:
    client = client_for(session_path, api_id, api_hash, runtime)
    await client.connect()
    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        if me is None:
            raise RuntimeError("Login succeeded but account info is empty")
        return me
    finally:
        await client.disconnect()


async def inspect_session(
    session_path: Path,
    api_id: int,
    api_hash: str,
    runtime: dict[str, str],
) -> tuple[str, User | None, str | None]:
    client = client_for(session_path, api_id, api_hash, runtime)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return "unauthorized", None, None
        me = await client.get_me()
        if me is None:
            return "empty", None, None
        return "active", me, None
    except SessionPasswordNeededError:
        return "twofa_required", None, None
    except Exception as exc:
        return "error", None, str(exc)
    finally:
        await client.disconnect()


def user_fields(user: User | None) -> dict[str, Any]:
    if user is None:
        return {
            "phone": None,
            "telegram_user_id": None,
            "username": None,
            "first_name": None,
            "last_name": None,
        }
    return {
        "phone": user.phone,
        "telegram_user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


def extract_verification_codes(text: str) -> list[str]:
    candidates: list[str] = []
    for match in CODE_RE.finditer(text or ""):
        code = re.sub(r"\D+", "", match.group(1))
        if 4 <= len(code) <= 8:
            candidates.append(code)
    if not candidates:
        return []
    if CODE_CONTEXT_RE.search(text or ""):
        return candidates
    return []


async def get_latest_telegram_code(
    session_path: Path,
    api_id: int,
    api_hash: str,
    runtime: dict[str, str],
    *,
    limit: int = 15,
) -> str | None:
    client = await _get_active_client(session_path, api_id, api_hash, runtime)
    await client(UpdateStatusRequest(offline=False))
    await client(GetStateRequest())

    for peer in VERIFICATION_CODE_PEERS:
        try:
            async for message in client.iter_messages(peer, limit=limit):
                text = message.message or ""
                codes = extract_verification_codes(text)
                if codes:
                    return codes[0]
        except Exception as exc:
            logger.info("Cannot read verification codes from peer %s: %s", peer, exc)
            continue
    return None
