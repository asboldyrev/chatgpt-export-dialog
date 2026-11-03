# ChatGPT Export Dialog

Экспортирует диалоги ChatGPT в Markdown. Python 3.10+, сторонние зависимости не требуются.

## Запуск

```bash
python3 export_dialogs.py /path/to/chatgpt-export.zip
python3 export_dialogs.py /path/to/unpacked-export -o /path/to/output
python3 export_dialogs.py /path/to/conversations.json --all-branches
```

Скрипт поддерживает ZIP, каталоги экспорта и файлы JSON. По умолчанию создаёт `dialogs_export` рядом с источником.

## Названия проектов

Создайте `projects.json` рядом с `export_dialogs.py`, взяв за основу `projects.example.json`:

```json
{
  "g-p-123456": "Hermes Agent",
  "g-p-abcdef": "RimWorld"
}
```

Ключ — точный ID проекта из `gizmo_id`, `project_id` или `conversation_template_id` в экспорте, значение — его название. Можно указать другой файл: `--projects /path/to/projects.json`. Если файл не задан и отсутствует рядом со скриптом, экспорт работает без сопоставлений. Если ID не найден в словаре, в качестве названия остаётся ID. Для двух проектов с одинаковым или совпавшим после нормализации названием автоматически добавляется уникальный суффикс, поэтому файлы не смешиваются.

## Структура

```text
dialogs_export/
├── README.md
├── projects/
│   └── Hermes_Agent/
│       └── 001_Название_диалога/
│           ├── dialogue.md
│           ├── assets/ (при наличии)
│           └── texts/ (при наличии)
└── chats/
    └── 001_Обычный_диалог/
        └── dialogue.md
```

В README результата отображаются названия проектов, их исходные ID и ссылки на диалоги. `--all-branches` сохраняет альтернативные ветки бесед. Исходный экспорт не изменяется.

Файл `export_dialogs_26.10.21.py` оставлен как историческая версия; актуальная точка входа — `export_dialogs.py`.
