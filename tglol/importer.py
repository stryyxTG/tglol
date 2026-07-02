from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import zipfile

from aiogram import Bot
from aiogram.types import Document

from tglol.config import Config
from tglol.db import add_account
from tglol.desktop_profile import generated_account_json, random_desktop_runtime, utc_now_iso
from tglol.json_utils import json_identity, load_json, pick_api, pick_twofa, runtime_from_json, write_json
from tglol.paths import safe_filename, unique_path
from tglol.telegram_service import inspect_session, user_fields


@dataclass(frozen=True)
class ImportResult:
    account_id: int
    status: str
    phone: str | None
    username: str | None
    source: str
    note: str | None = None


async def download_document(bot: Bot, document: Document, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    await bot.download(document, destination=destination)
    return destination


async def import_session_account(
    config: Config,
    *,
    session_path: Path,
    source_type: str,
    created_by: int | None,
    json_path: Path | None = None,
    twofa_password: str | None = None,
) -> ImportResult:
    uploaded_json = load_json(json_path) if json_path else None
    api_id, api_hash = pick_api(uploaded_json, config)
    runtime = runtime_from_json(uploaded_json) if uploaded_json else random_desktop_runtime()
    parsed_twofa = pick_twofa(uploaded_json)
    twofa_password = parsed_twofa if parsed_twofa is not None else twofa_password

    status, user, note = await inspect_session(session_path, api_id, api_hash, runtime)
    fields = user_fields(user)

    if uploaded_json:
        identity = json_identity(uploaded_json)
        for key, value in identity.items():
            if key == "session_file":
                continue
            if fields.get(key) in (None, "") and value not in (None, ""):
                fields[key] = value
        json_source = "uploaded"
        json_original_path = str(json_path)
        json_effective_path = str(json_path)
    else:
        json_source = "generated"
        json_original_path = None
        generated_name = session_path.with_suffix(".json").name
        effective_path = unique_path(config.json_dir, generated_name)
        generated = generated_account_json(
            config,
            runtime=runtime,
            twofa=twofa_password,
            session_file=session_path.name,
            phone=fields["phone"],
            user_id=fields["telegram_user_id"],
            username=fields["username"],
            first_name=fields["first_name"],
            last_name=fields["last_name"],
        )
        write_json(effective_path, generated)
        json_effective_path = str(effective_path)

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
            "json_original_path": json_original_path,
            "json_effective_path": json_effective_path,
            "json_source": json_source,
            "twofa_password": twofa_password,
            "source_type": source_type,
            "status": status,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        },
    )
    return ImportResult(account_id, status, fields["phone"], fields["username"], source_type, note)


def normalize_match_key(value: str) -> str:
    return "".join(ch for ch in value.lower().strip() if ch.isalnum())


def normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = Path(member.filename).name
            if not name:
                continue
            suffix = Path(name).suffix.lower()
            if suffix not in {".session", ".json"}:
                continue
            target = unique_path(destination, name)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def match_zip_files(session_files: list[Path], json_files: list[Path]) -> dict[Path, Path | None]:
    unmatched_jsons = set(json_files)
    result: dict[Path, Path | None] = {session: None for session in session_files}

    by_exact = {json_path.stem.lower(): json_path for json_path in json_files}
    for session in session_files:
        match = by_exact.get(session.stem.lower())
        if match and match in unmatched_jsons:
            result[session] = match
            unmatched_jsons.remove(match)

    by_normalized_name = {normalize_match_key(json_path.stem): json_path for json_path in unmatched_jsons}
    for session in session_files:
        if result[session]:
            continue
        match = by_normalized_name.get(normalize_match_key(session.stem))
        if match and match in unmatched_jsons:
            result[session] = match
            unmatched_jsons.remove(match)

    by_phone: dict[str, Path] = {}
    by_id: dict[str, Path] = {}
    by_session_file: dict[str, Path] = {}
    for json_path in list(unmatched_jsons):
        try:
            data = load_json(json_path)
        except Exception:
            continue
        phone = data.get("phone") or data.get("phone_number")
        user_id = data.get("user_id") or data.get("id")
        session_file = data.get("session_file")
        if phone:
            by_phone[normalize_phone(str(phone))] = json_path
        if user_id:
            by_id[normalize_match_key(str(user_id))] = json_path
        if session_file:
            by_session_file[Path(str(session_file)).stem.lower()] = json_path

    for session in session_files:
        if result[session]:
            continue
        candidates = [
            by_session_file.get(session.stem.lower()),
            by_phone.get(normalize_phone(session.stem)),
            by_id.get(normalize_match_key(session.stem)),
        ]
        for match in candidates:
            if match and match in unmatched_jsons:
                result[session] = match
                unmatched_jsons.remove(match)
                break

    unresolved_sessions = [session for session in session_files if result[session] is None]
    if len(unresolved_sessions) == 1 and len(unmatched_jsons) == 1:
        result[unresolved_sessions[0]] = next(iter(unmatched_jsons))
        unmatched_jsons.clear()
    elif len(unresolved_sessions) == len(unmatched_jsons) and unresolved_sessions:
        for session, json_path in zip(sorted(unresolved_sessions), sorted(unmatched_jsons)):
            result[session] = json_path
        unmatched_jsons.clear()

    return result


async def import_zip(
    config: Config,
    *,
    zip_path: Path,
    created_by: int | None,
) -> tuple[list[ImportResult], str]:
    batch_dir = unique_path(config.temp_dir, zip_path.stem)
    batch_dir.mkdir(parents=True, exist_ok=True)
    safe_extract_zip(zip_path, batch_dir)

    session_files = sorted(batch_dir.glob("*.session"))
    json_files = sorted(batch_dir.glob("*.json"))
    matches = match_zip_files(session_files, json_files)

    results: list[ImportResult] = []
    for session_tmp, json_tmp in matches.items():
        try:
            final_session = unique_path(config.sessions_dir, session_tmp.name)
            shutil.copy2(session_tmp, final_session)
            final_json = None
            if json_tmp:
                final_json = unique_path(config.json_dir, json_tmp.name)
                shutil.copy2(json_tmp, final_json)
            result = await import_session_account(
                config,
                session_path=final_session,
                json_path=final_json,
                source_type="zip",
                created_by=created_by,
            )
        except Exception as exc:
            result = ImportResult(
                account_id=0,
                status="error",
                phone=None,
                username=None,
                source="zip",
                note=f"{session_tmp.name}: {exc}",
            )
        results.append(result)

    imported_count = sum(1 for result in results if result.account_id)
    error_count = sum(1 for result in results if result.status == "error")
    summary = (
        f"SESSION: {len(session_files)}\n"
        f"JSON: {len(json_files)}\n"
        f"Импортировано: {imported_count}\n"
        f"Ошибок: {error_count}\n"
        f"Без JSON: {sum(1 for value in matches.values() if value is None)}"
    )
    return results, summary
