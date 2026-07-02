from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import User


CODE_RE = re.compile(r"(?<!\d)(\d[\d\s-]{2,14}\d)(?!\d)")
CODE_CONTEXT_RE = re.compile(r"\b(code|verification|verify|otp|passcode|парол|код|подтверж)\b", re.IGNORECASE)
VERIFICATION_CODE_PEERS = ("VerificationCodes", "@VerificationCodes")
logger = logging.getLogger(__name__)


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
    )


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
    client = client_for(session_path, api_id, api_hash, runtime)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("session is not authorized")

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
    finally:
        await client.disconnect()
