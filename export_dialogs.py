#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Экспорт бэкапа ChatGPT: каждый диалог — отдельная папка.
Проект определяется по полю в данных: gizmo_id или conversation_template_id.
Если поле задано — диалог попадает в папку проекта; иначе — в отдельные диалоги.
- dialogue.md — сам диалог в markdown
- Длинные тексты (>2500 символов) — в отдельных txt файлах
- Изображения копируются в папку диалога (assets/)
"""

import argparse
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONVERSATIONS_JSON = BASE / "conversations.json"
OUTPUT_DIR = BASE / "dialogs_export"
LONG_TEXT_THRESHOLD = 2500
MAX_TITLE_LEN = 80


def get_project_id(conv: dict) -> str | None:
    """
    Определяет проект по данным диалога.
    В бэкапе ChatGPT проект задаётся полем gizmo_id (или conversation_template_id).
    Возвращает ID проекта для имени папки или None — диалог вне проекта.
    """
    raw = conv.get("gizmo_id") or conv.get("conversation_template_id") or conv.get("metadata", {}).get("gizmo_id")
    if not raw or not str(raw).strip():
        return None
    return str(raw).strip()


def sanitize_folder_name(s: str) -> str:
    """Имя папки проекта: только безопасные символы."""
    # Заменяем все небезопасные символы на подчеркивание
    # Включаем пробелы, управляющие символы, запрещенные для файловой системы
    s = re.sub(r'[<>:"/\\|?*\s\r\n\t]', "_", s)
    # Удаляем ведущие и конечные подчеркивания и точки
    s = s.strip("_.")
    # Заменяем множественные подчеркивания на одно
    s = re.sub(r'_+', '_', s)
    # Если после очистки строка пуста, используем значение по умолчанию
    if not s:
        s = "project"
    # Ограничиваем длину
    return s[:MAX_TITLE_LEN]


def sanitize_filename(s: str) -> str:
    # Заменяем все небезопасные символы на подчеркивание (включая пробелы)
    s = re.sub(r'[<>:"/\\|?*\s\r\n\t]', "_", s)
    # Удаляем ведущие и конечные подчеркивания и точки
    s = s.strip("_.")
    # Заменяем множественные подчеркивания на одно
    s = re.sub(r'_+', '_', s)
    # Если после очистки строка пуста, используем значение по умолчанию
    if not s:
        s = "untitled"
    # Ограничиваем длину
    return s[:MAX_TITLE_LEN]


def build_file_map(base_path=None):
    """Строит маппинг file_id / asset_pointer -> локальный путь."""
    if base_path is None:
        base_path = BASE
    file_map = {}
    
    def add_mapping(fid, path):
        """Добавляет отображение с дедупликацией: выбираем более полный путь."""
        existing = file_map.get(fid)
        if existing is None:
            file_map[fid] = path
        else:
            # Выбираем путь с большей длиной (эвристика: более полный путь)
            if len(str(path)) > len(str(existing)):
                file_map[fid] = path
            # Иначе оставляем существующий
    
    # Файлы в корневой папке (префикс file-)
    for f in os.listdir(base_path):
        path = base_path / f
        if path.is_file() and f.startswith("file-"):
            parts = f.split("-", 2)
            if len(parts) >= 2:
                fid = "file-" + parts[1]
                add_mapping(fid, path)
    
    # Файлы в user-FpDwdx0tII6eDxp5lApJkSsL (префикс file_)
    user_dir = base_path / "user-FpDwdx0tII6eDxp5lApJkSsL"
    if user_dir.is_dir():
        for f in os.listdir(user_dir):
            if f.startswith("file_"):
                # sediment://file_00000000c46061f9bd587888eb5e6ce5
                h = f.split("-")[0]
                add_mapping("sediment://" + h, user_dir / f)
    
    # Файлы в dalle-generations
    dalle_dir = base_path / "dalle-generations"
    if dalle_dir.is_dir():
        for f in os.listdir(dalle_dir):
            path = dalle_dir / f
            if path.is_file() and f.startswith("file-"):
                parts = f.split("-", 2)
                if len(parts) >= 2:
                    fid = "file-" + parts[1]
                    add_mapping(fid, path)
    
    return file_map


def resolve_asset_path(part: dict, file_map: dict) -> str | None:
    if not isinstance(part, dict):
        return None
    ptr = part.get("asset_pointer")
    if not ptr:
        return None
    # file-service://file-85nHsLezMWbFESUDBRMhQE
    if "file-service://" in str(ptr):
        fid = ptr.replace("file-service://", "").strip()
        return str(file_map.get(fid)) if file_map.get(fid) else None
    if str(ptr).startswith("sediment://"):
        return str(file_map.get(ptr)) if file_map.get(ptr) else None
    return None


def get_ordered_message_ids(mapping: dict) -> list[str]:
    """
    Возвращает список ID сообщений в хронологическом порядке, исключая системные.
    Собирает все сообщения из mapping, имеющие message с ролью не system,
    и сортирует по времени создания (create_time) если оно есть, иначе по ID.
    """
    candidates = []
    for nid, node in mapping.items():
        msg = node.get("message")
        if not msg:
            continue
        role = msg.get("author", {}).get("role")
        if role == "system":
            continue
        # Получаем время создания
        create_time = msg.get("create_time")
        # Если время None, используем 0
        candidates.append((nid, create_time if create_time is not None else 0))
    
    # Сортировка по времени (возрастание), затем по ID для стабильности
    candidates.sort(key=lambda x: (x[1], x[0]))
    return [cid for cid, _ in candidates]


def extract_parts_text_and_images(msg: dict, file_map: dict) -> tuple[list[str], list[tuple[str, str]]]:
    """Возвращает (список текстовых частей, список (markdown_ref, local_path))."""
    texts = []
    images = []
    content = msg.get("content") or {}
    parts = content.get("parts") or []
    for p in parts:
        if isinstance(p, str) and p.strip():
            texts.append(p.strip())
        elif isinstance(p, dict):
            if p.get("content_type") == "image_asset_pointer":
                path = resolve_asset_path(p, file_map)
                if path and os.path.isfile(path):
                    images.append((path, path))
            # текст внутри dict не трогаем
    return texts, images


def export_conversation(conv: dict, folder_name: str, file_map: dict, project_folder: str | None = None, output_dir: Path = OUTPUT_DIR) -> None:
    """Экспортирует один диалог. project_folder — папка проекта внутри output_dir или None для отдельного диалога."""
    title = (conv.get("title") or "New chat").strip() or "Без названия"
    cid = conv.get("id") or conv.get("conversation_id") or "conv"
    create_ts = conv.get("create_time")
    if create_ts:
        try:
            dt = datetime.fromtimestamp(create_ts)
            date_str = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            date_str = ""
    else:
        date_str = ""

    if project_folder:
        out_path = output_dir / project_folder / folder_name
    else:
        out_path = output_dir / folder_name
    out_path.mkdir(parents=True, exist_ok=True)
    assets_path = out_path / "assets"
    assets_path.mkdir(exist_ok=True)
    texts_path = out_path / "texts"
    texts_path.mkdir(exist_ok=True)

    mapping = conv.get("mapping") or {}
    order = get_ordered_message_ids(mapping)
    if not order:
        return

    md_lines = [
        f"# {title}",
        "",
        f"*Дата: {date_str}*",
        f"*ID: {cid}*",
        "",
        "---",
        "",
    ]
    image_counter = 0
    text_file_counter = 0
    copied_images = set()

    for pos, nid in enumerate(order):
        node = mapping.get(nid)
        if not node:
            continue
        msg = node.get("message")
        if not msg:
            continue
        author = msg.get("author") or {}
        role = author.get("role") or "user"
        if role == "system":
            continue
        name = "Пользователь" if role == "user" else "Ассистент"
        texts, image_list = extract_parts_text_and_images(msg, file_map)

        md_lines.append(f"## {name}")
        md_lines.append("")

        for t in texts:
            if len(t) > LONG_TEXT_THRESHOLD:
                text_file_counter += 1
                fn = f"message_{pos+1}_{text_file_counter}.txt"
                txt_path = texts_path / fn
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(t)
                md_lines.append(f"*[Длинный текст сохранён в отдельный файл: [texts/{fn}](texts/{fn})]*")
                md_lines.append("")
                md_lines.append("<details><summary>Краткий фрагмент</summary>")
                md_lines.append("")
                md_lines.append("```")
                md_lines.append(t[:500].replace("```", "` ` `") + ("..." if len(t) > 500 else ""))
                md_lines.append("```")
                md_lines.append("</details>")
                md_lines.append("")
            else:
                md_lines.append(t)
                md_lines.append("")

        for src_path, _ in image_list:
            if src_path in copied_images:
                continue
            image_counter += 1
            ext = Path(src_path).suffix or ".png"
            dest_name = f"image_{image_counter}{ext}"
            dest_path = assets_path / dest_name
            try:
                shutil.copy2(src_path, dest_path)
                copied_images.add(src_path)
            except Exception:
                dest_name = ""
            if dest_name:
                md_lines.append(f"![Изображение](assets/{dest_name})")
                md_lines.append("")

        md_lines.append("")

    dialogue_md = out_path / "dialogue.md"
    with open(dialogue_md, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    # Пустые папки texts можно не оставлять — но оставим для единообразия
    if not list(texts_path.iterdir()):
        texts_path.rmdir()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Экспорт диалогов ChatGPT из папки бэкапа.')
    parser.add_argument('path', nargs='?', default='.', help='Путь к папке с бэкапом (по умолчанию текущая директория)')
    args = parser.parse_args()

    base_path = Path(args.path).resolve()
    conversations_json = base_path / "conversations.json"
    output_dir = base_path / "dialogs_export"

    if not conversations_json.exists():
        print(f"Файл conversations.json не найден в {base_path}.")
        return
    with open(conversations_json, "r", encoding="utf-8") as f:
        conversations = json.load(f)
    if not isinstance(conversations, list):
        conversations = [conversations]

    def sort_key(c):
        t = c.get("create_time") or 0
        return (t, c.get("id", ""))

    conversations.sort(key=sort_key)

    # Распределение по проектам: проект задаётся полем gizmo_id / conversation_template_id
    by_project = {}  # project_id -> [conv, ...]
    standalone = []
    for conv in conversations:
        project_id = get_project_id(conv)
        if project_id:
            by_project.setdefault(project_id, []).append(conv)
        else:
            standalone.append(conv)

    file_map = build_file_map(base_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    exported = 0

    for project_id, convs in sorted(by_project.items()):
        project_folder = sanitize_folder_name(project_id)
        (output_dir / project_folder).mkdir(parents=True, exist_ok=True)
        convs.sort(key=sort_key)
        for local_i, conv in enumerate(convs, 1):
            title = (conv.get("title") or "New chat").strip() or "Без названия"
            folder_name = f"{local_i:03d}_{sanitize_filename(title)}"
            try:
                export_conversation(conv, folder_name, file_map, project_folder=project_folder, output_dir=output_dir)
                exported += 1
                print(f"  [{project_folder}] {folder_name} — {title[:50]}")
            except Exception as e:
                print(f"Ошибка: {project_folder}/{folder_name}: {e}")
                import traceback
                traceback.print_exc()

    for i, conv in enumerate(standalone, 1):
        title = (conv.get("title") or "New chat").strip() or "Без названия"
        folder_name = f"{i:03d}_{sanitize_filename(title)}"
        try:
            export_conversation(conv, folder_name, file_map, project_folder=None, output_dir=output_dir)
            exported += 1
            print(f"  (отдельный) {folder_name} — {title[:50]}")
        except Exception as e:
            print(f"Ошибка: {folder_name}: {e}")
            import traceback
            traceback.print_exc()

    # README: структура и пояснение
    write_export_readme(by_project, standalone, output_dir=output_dir)

    print(f"\nГотово. Экспортировано {exported} диалогов.")
    print(f"  Проектов (по gizmo_id/conversation_template_id): {len(by_project)}")
    print(f"  Отдельных диалогов (без проекта): {len(standalone)}")
    print(f"Папка: {output_dir}")


def write_export_readme(by_project: dict, standalone: list, output_dir: Path = OUTPUT_DIR) -> None:
    """Пишет README.md в output_dir с описанием структуры."""
    def sort_key(c):
        t = c.get("create_time") or 0
        return (t, c.get("id", ""))

    lines = [
        "# Экспорт диалогов ChatGPT",
        "",
        "Проект в данных задаётся полем **gizmo_id** (или **conversation_template_id**) у диалога.",
        "Если поле задано — диалог лежит в папке проекта; иначе — в «Отдельные диалоги».",
        "",
        "В каждой папке диалога: `dialogue.md`, при необходимости `texts/`, `assets/`.",
        "",
        "---",
        "",
        "## Проекты (по gizmo_id)",
        "",
    ]
    for project_id in sorted(by_project.keys()):
        folder = sanitize_folder_name(project_id)
        convs = by_project[project_id]
        convs_sorted = sorted(convs, key=sort_key)
        lines.append(f"### {folder}")
        lines.append(f"*(id: `{project_id}`)*")
        lines.append("")
        for i, conv in enumerate(convs_sorted, 1):
            title = (conv.get("title") or "New chat").strip() or "Без названия"
            sub = f"{i:03d}_{sanitize_filename(title)}"
            lines.append(f"- [{title}]({folder}/{sub}/dialogue.md)")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Отдельные диалоги")
    lines.append("")
    standalone_sorted = sorted(standalone, key=sort_key)
    for i, conv in enumerate(standalone_sorted, 1):
        title = (conv.get("title") or "New chat").strip() or "Без названия"
        sub = f"{i:03d}_{sanitize_filename(title)}"
        lines.append(f"- [{title}]({sub}/dialogue.md)")
    lines.append("")
    (output_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
