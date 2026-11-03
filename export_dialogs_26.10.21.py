#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Экспорт разговоров ChatGPT в отдельные Markdown-папки.

Поддерживаются каталоги экспорта, одиночные JSON, внешние ZIP и вложенные ZIP,
разбитые conversations-NNN.json, mapping/current_node и плоские messages,
file-service:// / sediment:// указатели, файлы .dat и attachments.

Python 3.10+, только стандартная библиотека. Исходные архивы не изменяются.

Примеры:
    python export_dialogs_updated.py ~/Downloads/chatgpt-export.zip
    python export_dialogs_updated.py ~/Downloads/unpacked_export -o ~/Documents/dialogs
    python export_dialogs_updated.py conversations.json --all-branches
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

LONG_TEXT_THRESHOLD = 2500
MAX_TITLE_LEN = 80
MAX_NESTING = 6
# Защита от распаковки архивов-бомб; реальные экспорты могут быть очень большими.
MAX_UNCOMPRESSED_TOTAL = 100 * (1024 ** 3)
CONVERSATIONS_PATTERN = re.compile(r"^conversations(?:[-_]\d+)?\.json$", re.IGNORECASE)
FILE_DASH_PATTERN = re.compile(r"(file-[A-Za-z0-9]+)")
FILE_UNDER_PATTERN = re.compile(r"(file_[0-9a-fA-F]{20,})")
MEDIA_TYPES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
    ".mp3", ".m4a", ".ogg", ".wav", ".flac", ".mp4", ".mov", ".webm",
}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def log_warning(message: str) -> None:
    print(f"Предупреждение: {message}", file=sys.stderr)


def sanitize_filename(value: object, fallback: str = "untitled") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', "_", str(value or ""))
    text = re.sub(r"_+", "_", text).strip("_. ")[:MAX_TITLE_LEN].rstrip("_. ")
    if not text:
        text = fallback
    if text.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        text = "_" + text
    return text


def markdown_escape(value: object) -> str:
    return re.sub(r"([\\`*\[\]<>])", r"\\\1", str(value))


def md_url(path: Path | str) -> str:
    from urllib.parse import quote
    return quote(str(path).replace("\\", "/"), safe="/-_.")


def to_timestamp(value: object) -> float:
    if value is None:
        return 0.0
    try:
        stamp = float(value)
        return stamp / 1000 if abs(stamp) > 1e11 else stamp
    except (ValueError, TypeError):
        pass
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
        except ValueError:
            pass
    return 0.0


def format_date(value: object) -> str:
    stamp = to_timestamp(value)
    if not stamp:
        return "не указана"
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return "не указана"


def get_project_id(conv: dict) -> str | None:
    meta = conv.get("metadata") if isinstance(conv.get("metadata"), dict) else {}
    for value in (conv.get("gizmo_id"), conv.get("project_id"), conv.get("conversation_template_id"),
                  meta.get("project_id"), meta.get("gizmo_id"), meta.get("conversation_template_id")):
        if value and str(value).strip():
            return str(value).strip()
    return None


def is_conv(obj: object) -> bool:
    return isinstance(obj, dict) and (isinstance(obj.get("mapping"), dict) or isinstance(obj.get("messages"), list))


def iter_conversations(doc: object):
    if is_conv(doc):
        yield doc
    elif isinstance(doc, list):
        for item in doc:
            if is_conv(item):
                yield item
    elif isinstance(doc, dict):
        for key in ("conversations", "items", "data"):
            nested = doc.get(key)
            if isinstance(nested, (list, dict)):
                yield from iter_conversations(nested)


def safe_extract(zf: zipfile.ZipFile, dest: Path, total: list[int]) -> None:
    """Не разрешаем выход за каталог назначения или ZIP-симлинки."""
    for info in zf.infolist():
        # ZipInfo.external_attr: POSIX type 0o120000 is a symlink.
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            log_warning(f"Пропущен символический линк: {info.filename}")
            continue
        relative = Path(info.filename.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            log_warning(f"Пропущен небезопасный путь в ZIP: {info.filename}")
            continue
        target = dest / relative
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        total[0] += info.file_size
        if total[0] > MAX_UNCOMPRESSED_TOTAL:
            raise ValueError("Превышен лимит распаковки 100 ГиБ; проверьте архив")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)


def is_export_archive(path: Path) -> bool:
    """Не раскрываем ZIP-документы, загруженные пользователем как вложения."""
    return bool(re.match(r"^(conversations|files|openai|chatgpt|export|part[-_])", path.name, re.I))


def prepare_roots(source: Path, work_dir: Path) -> list[Path]:
    """Подключаем исходный каталог и вложенные ZIP; извлекаем только во временную папку."""
    if source.is_file() and source.suffix.lower() == ".json":
        return [source.parent]
    roots: list[Path] = []
    archives: list[tuple[Path, int]] = []
    visited: set[Path] = set()
    if source.is_dir():
        roots.append(source)
        archives.extend((item, 0) for item in source.rglob("*.zip") if item.is_file() and is_export_archive(item))
    elif source.suffix.lower() == ".zip":
        archives.append((source, 0))
    else:
        raise ValueError("Укажите каталог экспорта, JSON или ZIP")

    total = [0]
    archive_count = 0
    while archives:
        archive, level = archives.pop(0)
        if archive in visited:
            continue
        visited.add(archive)
        if level > MAX_NESTING:
            log_warning(f"Слишком глубокая вложенность ZIP: {archive.name}")
            continue
        archive_count += 1
        target = work_dir / f"archive_{archive_count:05d}"
        try:
            with zipfile.ZipFile(archive) as zf:
                safe_extract(zf, target, total)
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            log_warning(f"Не удалось прочитать ZIP {archive}: {exc}")
            continue
        roots.append(target)
        for child in target.rglob("*.zip"):
            if child.is_file() and is_export_archive(child):
                archives.append((child, level + 1))
    return roots


def find_conversations(roots: list[Path], source: Path, output_dir: Path) -> list[dict]:
    paths: set[Path] = set()
    if source.is_file() and source.suffix.lower() == ".json":
        paths.add(source)
    else:
        for root in roots:
            for p in root.rglob("*.json"):
                if output_dir == p or output_dir in p.parents:
                    continue
                if CONVERSATIONS_PATTERN.fullmatch(p.name):
                    paths.add(p)
    if not paths:
        raise FileNotFoundError("Не найдены conversations.json или conversations-NNN.json")
    conversations: dict[str, dict] = {}
    for path in sorted(paths):
        print(f"Чтение: {path.name}")
        try:
            with path.open("r", encoding="utf-8-sig") as stream:
                doc = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            log_warning(f"Пропущен JSON {path}: {exc}")
            continue
        for conv in iter_conversations(doc):
            cid = str(conv.get("id") or conv.get("conversation_id") or "")
            if not cid:
                cid = hashlib.sha1(json.dumps(conv, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
            previous = conversations.get(cid)
            if previous is None:
                conversations[cid] = conv
            else:
                def score(c):
                    return (len(c.get("mapping") or c.get("messages") or []), to_timestamp(c.get("update_time")))
                if score(conv) > score(previous):
                    conversations[cid] = conv
    if not conversations:
        raise ValueError("В файлах JSON не найдено диалогов c mapping или messages")
    return sorted(conversations.values(), key=lambda c: (to_timestamp(c.get("create_time")), str(c.get("id") or c.get("conversation_id") or "")))


def asset_aliases(path: Path, root: Path) -> set[str]:
    aliases = {path.name.casefold(), str(path.relative_to(root)).replace("\\", "/").casefold()}
    for pattern in (FILE_DASH_PATTERN, FILE_UNDER_PATTERN):
        match = pattern.search(path.name)
        if match:
            aliases.add(match.group(1).casefold())
    # Файл file-...dat также доступен по полному имени без .dat.
    if path.suffix.lower() == ".dat":
        aliases.add(path.stem.casefold())
    return aliases


def build_file_map(roots: list[Path], output_dir: Path) -> dict[str, Path]:
    lookup: dict[str, Path] = {}
    count = 0
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or output_dir in path.parents:
                continue
            if path.suffix.lower() in {".json", ".zip", ".html"}:
                continue
            count += 1
            for key in asset_aliases(path, root):
                # Полное имя файла обычно надёжнее алиаса file-id.
                lookup.setdefault(key, path)
    print(f"Файлов вложений проиндексировано: {count}")
    return lookup


def asset_candidates(value: object) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    raw = unquote(value.strip()).replace("\\", "/")
    candidates = [raw.casefold()]
    # file-service://file-XXX, sediment://file_XXX, https://.../file-XXX
    parsed = urlparse(raw)
    if parsed.scheme:
        if parsed.netloc:
            candidates.append(parsed.netloc.casefold())
        if parsed.path:
            candidates.append(parsed.path.lstrip("/").casefold())
    candidates.append(raw.rsplit("/", 1)[-1].casefold())
    for pattern in (FILE_DASH_PATTERN, FILE_UNDER_PATTERN):
        match = pattern.search(raw)
        if match:
            candidates.append(match.group(1).casefold())
    return list(dict.fromkeys(candidates))


def resolve_asset(obj: dict, lookup: dict[str, Path]) -> Path | None:
    for key in ("asset_pointer", "file_id", "id", "path", "file_path", "filename", "name", "url"):
        value = obj.get(key)
        if isinstance(value, dict):
            value = value.get("url") or value.get("path")
        for candidate in asset_candidates(value):
            if candidate in lookup:
                return lookup[candidate]
    return None


def sniff_extension(source: Path) -> str:
    ext = source.suffix.lower()
    if ext not in {"", ".dat", ".bin"}:
        return ext if re.fullmatch(r"\.[a-z0-9]{1,10}", ext) else ".bin"
    try:
        with source.open("rb") as file:
            header = file.read(64)
    except OSError:
        return ".bin"
    checks = (
        (b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"),
        (b"GIF87a", ".gif"), (b"GIF89a", ".gif"), (b"%PDF-", ".pdf"),
        (b"PK\x03\x04", ".zip"), (b"ID3", ".mp3"),
        (b"OggS", ".ogg"), (b"fLaC", ".flac"),
    )
    for magic, extension in checks:
        if header.startswith(magic):
            return extension
    if header.startswith(b"RIFF"):
        if header[8:12] == b"WEBP":
            return ".webp"
        if header[8:12] == b"WAVE":
            return ".wav"
    if header[4:8] == b"ftyp":
        return ".m4a" if b"M4A" in header[:20] else ".mp4"
    if header.startswith((b"<?xml", b"<svg")) and b"<svg" in header:
        return ".svg"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return ".webm"
    return ".bin"


def get_ordered_messages(conv: dict, all_branches: bool = False) -> list[dict]:
    mapping = conv.get("mapping")
    if isinstance(mapping, dict) and mapping:
        ids = []
        current = conv.get("current_node")
        if not all_branches and current in mapping:
            seen: set[str] = set()
            while current in mapping and current not in seen:
                seen.add(current)
                ids.append(current)
                current = mapping[current].get("parent") if isinstance(mapping[current], dict) else None
            ids.reverse()
        else:
            if not all_branches:
                log_warning(f"У диалога {conv.get('title')} нет корректного current_node: сохранены все ветки")
            ids = sorted(mapping, key=lambda n: (
                to_timestamp(((mapping[n] or {}).get("message") or {}).get("create_time")) if isinstance(mapping[n], dict) else 0,
                str(n),
            ))
        return [mapping[n]["message"] for n in ids if isinstance(mapping[n], dict) and isinstance(mapping[n].get("message"), dict)]
    messages = conv.get("messages") or []
    page_info = conv.get("page_info") or {}
    if isinstance(page_info, dict) and page_info.get("has_previous_page"):
        log_warning(f"У диалога {conv.get('title')} есть другие страницы сообщений: экспорт может быть неполным")
    result = []
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry
        if isinstance(msg, dict):
            result.append(msg)
    return result


def extract_text_and_media(msg: dict) -> tuple[list[tuple[str, object]], list[dict]]:
    """Сохраняем порядок мультимодальных частей, а вложения возвращаем отдельно."""
    content = msg.get("content") or {}
    result: list[tuple[str, object]] = []
    if isinstance(content, str):
        if content.strip():
            result.append(("text", content.strip()))
    elif isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            for p in parts:
                if isinstance(p, str):
                    if p.strip():
                        result.append(("text", p.strip()))
                elif isinstance(p, dict):
                    kind = str(p.get("content_type") or p.get("type") or "")
                    if kind in {"image_asset_pointer", "audio_asset_pointer", "video_asset_pointer", "file_asset_pointer"} or p.get("asset_pointer"):
                        result.append(("media", p))
                    elif kind in {"text", "input_text", "text_part"} and isinstance(p.get("text"), str):
                        result.append(("text", p["text"]))
                    elif kind in {"audio_transcription", "text_audio", "text_sentinel", "transcription"}:
                        val = p.get("text") or p.get("transcript") or p.get("transcription")
                        if isinstance(val, str):
                            result.append(("text", val))
                    elif isinstance(p.get("text"), str):
                        result.append(("text", p["text"]))
                    elif kind:
                        result.append(("note", f"[Неподдерживаемая часть: {kind}]"))
        elif isinstance(content.get("text"), str):
            result.append(("text", content["text"]))
        elif isinstance(content.get("content"), str):
            result.append(("text", content["content"]))
        elif isinstance(content.get("transcript"), str):
            result.append(("text", content["transcript"]))
        elif isinstance(content.get("summary"), str):
            result.append(("text", content["summary"]))
        elif content.get("content_type"):
            result.append(("note", f"[Неподдерживаемое содержимое: {content['content_type']}]") )
    meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    attachments = meta.get("attachments") or msg.get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []
    return result, [a for a in attachments if isinstance(a, dict)]


def write_long_text(text: str, texts_dir: Path, ordinal: int, number: int, lines: list[str]) -> None:
    if len(text) <= LONG_TEXT_THRESHOLD:
        lines.extend((text, ""))
        return
    texts_dir.mkdir(parents=True, exist_ok=True)
    filename = f"message_{ordinal}_{number}.txt"
    (texts_dir / filename).write_text(text, encoding="utf-8")
    lines.extend((f"*Длинный текст: [texts/{filename}](texts/{filename})*", "",
                  "<details><summary>Первые 500 символов</summary>", "",
                  "~~~~text", text[:500].replace("~~~~", "~ ~ ~ ~") + ("…" if len(text) > 500 else ""),
                  "~~~~", "", "</details>", ""))


def export_conversation(conv: dict, out_path: Path, lookup: dict[str, Path], all_branches: bool = False) -> tuple[bool, int]:
    messages = get_ordered_messages(conv, all_branches)
    if not messages:
        return False, 0
    out_path.mkdir(parents=True, exist_ok=True)
    assets_dir = out_path / "assets"
    texts_dir = out_path / "texts"
    title = str(conv.get("title") or "Без названия").strip()
    cid = str(conv.get("id") or conv.get("conversation_id") or "не указан")
    lines = [f"# {markdown_escape(title)}", "", f"*Дата: {format_date(conv.get('create_time'))}*",
             f"*ID: `{markdown_escape(cid)}`*", "", "---", ""]
    used_assets: dict[Path, str] = {}
    missing_count = 0
    text_counter = 0
    message_no = 0

    def attach(part: dict, explicit_name: str = "", image_hint: bool = False) -> str:
        nonlocal missing_count
        src = resolve_asset(part, lookup)
        description = explicit_name or str(part.get("name") or part.get("filename") or part.get("asset_pointer") or part.get("id") or "вложение")
        if not src:
            missing_count += 1
            return f"*Вложение не найдено в экспорте: {markdown_escape(description)}*"
        if src not in used_assets:
            assets_dir.mkdir(exist_ok=True)
            extension = sniff_extension(src)
            # Для .dat оригинальное имя из attachments точнее сигнатуры ZIP
            # (например, документ .docx внутри тоже начинается с PK).
            named_extension = Path(explicit_name).suffix.lower() if explicit_name else ""
            if src.suffix.lower() in {"", ".dat", ".bin"} and re.fullmatch(r"\.[a-z0-9]{1,10}", named_extension):
                extension = named_extension
            basename = sanitize_filename(Path(explicit_name).stem, "file") if explicit_name else "asset"
            dest_name = f"{len(used_assets)+1:04d}_{basename}{extension}"
            try:
                shutil.copy2(src, assets_dir / dest_name)
            except OSError as exc:
                missing_count += 1
                return f"*Ошибка копирования вложения {markdown_escape(description)}: {exc}*"
            used_assets[src] = dest_name
        file_name = used_assets[src]
        target = f"assets/{md_url(file_name)}"
        if image_hint or Path(file_name).suffix.lower() in IMAGE_TYPES:
            return f"![{markdown_escape(explicit_name or 'Изображение')}]({target})"
        return f"[Файл: {markdown_escape(explicit_name or src.name)}]({target})"

    for msg in messages:
        author = msg.get("author") or {}
        role = author.get("role") if isinstance(author, dict) else author
        if role not in ("user", "assistant"):
            continue
        metadata = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
        if metadata.get("is_visually_hidden_from_conversation"):
            continue
        if role == "assistant" and msg.get("recipient") not in (None, "all", ""):
            continue
        parts, attachments = extract_text_and_media(msg)
        if not parts and not attachments:
            continue
        message_no += 1
        lines.extend((f"## {'Пользователь' if role == 'user' else 'Ассистент'}", ""))
        seen_message_assets: set[Path] = set()
        for kind, payload in parts:
            if kind == "text":
                text_counter += 1
                write_long_text(str(payload), texts_dir, message_no, text_counter, lines)
            elif kind == "media" and isinstance(payload, dict):
                path = resolve_asset(payload, lookup)
                if path:
                    seen_message_assets.add(path)
                hint = payload.get("content_type") == "image_asset_pointer"
                lines.extend((attach(payload, image_hint=hint), ""))
            elif kind == "note":
                lines.extend((str(payload), ""))
        for attachment in attachments:
            path = resolve_asset(attachment, lookup)
            if path and path in seen_message_assets:
                continue
            if path:
                seen_message_assets.add(path)
            name = str(attachment.get("name") or attachment.get("filename") or "")
            lines.extend((attach(attachment, explicit_name=name), ""))
    (out_path / "dialogue.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return True, missing_count


def write_export_readme(by_project: dict[str, list[tuple[dict, Path]]], standalone: list[tuple[dict, Path]],
                        output_dir: Path, missing_count: int) -> None:
    lines = ["# Экспорт диалогов ChatGPT", "", "Каждый диалог хранится в `dialogue.md`.",
             "Длинные сообщения — в `texts/`, найденные вложения — в `assets/`.",
             "Проекты сгруппированы по `gizmo_id`, `project_id` или `conversation_template_id`.", ""]
    if missing_count:
        lines.extend((f"*Внимание: не удалось найти или скопировать {missing_count} вложений.*", ""))
    lines.extend(("## Проекты", ""))
    for project_id, group in sorted(by_project.items()):
        lines.extend((f"### {markdown_escape(project_id)}", ""))
        for conv, rel in group:
            lines.append(f"- [{markdown_escape(conv.get('title') or 'Без названия')}]({md_url(rel / 'dialogue.md')})")
        lines.append("")
    lines.extend(("## Отдельные диалоги", ""))
    for conv, rel in standalone:
        lines.append(f"- [{markdown_escape(conv.get('title') or 'Без названия')}]({md_url(rel / 'dialogue.md')})")
    lines.append("")
    (output_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перевод архива ChatGPT (ZIP, JSON, каталог) в папки с Markdown.")
    parser.add_argument("path", nargs="?", default=".", help="Каталог, JSON или ZIP с данными экспорта")
    parser.add_argument("-o", "--output", help="Папка результата (по умолчанию dialogs_export рядом с источником)")
    parser.add_argument("--all-branches", action="store_true", help="Выгружать все ветки mapping, включая альтернативные ответы")
    args = parser.parse_args(argv)
    source = Path(args.path).expanduser().resolve()
    if not source.exists():
        parser.error(f"Путь не найден: {source}")
    output_dir = Path(args.output).expanduser().resolve() if args.output else (
        source / "dialogs_export" if source.is_dir() else source.parent / "dialogs_export")
    if source.is_dir() and output_dir == source:
        parser.error("Папка результата должна отличаться от исходного каталога")
    with tempfile.TemporaryDirectory(prefix="chatgpt_export_") as tmp:
        roots = prepare_roots(source, Path(tmp))
        conversations = find_conversations(roots, source, output_dir)
        lookup = build_file_map(roots, output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        groups: dict[str, list[dict]] = defaultdict(list)
        standalone_convs: list[dict] = []
        for conv in conversations:
            project = get_project_id(conv)
            (groups[project] if project else standalone_convs).append(conv)

        processed_projects: dict[str, list[tuple[dict, Path]]] = defaultdict(list)
        processed_standalone: list[tuple[dict, Path]] = []
        exported = missing = 0
        reserved_project_names: set[str] = set()
        for project_id in sorted(groups):
            base_name = sanitize_filename(project_id, "project")
            name = base_name
            if name.casefold() in reserved_project_names:
                name += "_" + hashlib.sha1(project_id.encode()).hexdigest()[:8]
            reserved_project_names.add(name.casefold())
            for i, conv in enumerate(groups[project_id], 1):
                rel = Path(name) / f"{i:03d}_{sanitize_filename(conv.get('title') or 'New_chat')}"
                try:
                    created, not_found = export_conversation(conv, output_dir / rel, lookup, args.all_branches)
                    if created:
                        exported += 1
                        missing += not_found
                        processed_projects[project_id].append((conv, rel))
                        print(f"  [{name}] {rel.name}")
                except Exception as exc:
                    log_warning(f"Ошибка диалога {rel}: {exc}")
        for i, conv in enumerate(standalone_convs, 1):
            rel = Path(f"{i:03d}_{sanitize_filename(conv.get('title') or 'New_chat')}")
            try:
                created, not_found = export_conversation(conv, output_dir / rel, lookup, args.all_branches)
                if created:
                    exported += 1
                    missing += not_found
                    processed_standalone.append((conv, rel))
                    print(f"  (отдельный) {rel.name}")
            except Exception as exc:
                log_warning(f"Ошибка диалога {rel}: {exc}")
        write_export_readme(processed_projects, processed_standalone, output_dir, missing)
        print(f"\nГотово. Экспортировано: {exported} из {len(conversations)}. "
              f"Проектов: {len(processed_projects)}. Отдельных: {len(processed_standalone)}. "
              f"Ненайденных вложений: {missing}.\nРезультат: {output_dir}")
        return 0 if exported else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
