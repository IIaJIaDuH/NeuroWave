# NeuroWave — обязательные гейты для PR (с 17.11.2026)

**Назначение:** уменьшить риск быстрого AI-assisted кодинга, в котором объём diff растёт быстрее человеческой проверки. **Одна ветка → одна задача → отдельный PR → воспроизводимые проверки → ревью → merge.**

## 1. Обязательные сведения в PR

- **Изменённый контракт.** Что пользователь или API видит до и после? Если API внешне не меняется — прямое указание `no external behavior change`.
- **Scope.** Конкретные файлы и почему они нужны. Файлы вне scope требуют объяснения; смена технологии/объёма в процессе — отдельный PR.
- **Воспроизводимость.** Исходный дефект или тест, который упал до фикса, прошёл после. Для чистого refactor — сравнение observable behavior.
- **CI.** Явно отметить `passed` / `failed` / `skipped` / `not run`; не выдавать `Qt offscreen` за физический GUI/звук, а `--ao=null` за слышимый Bluetooth/HDMI.
- **Ревью diff.** Автор обязан изучить `git diff --check`, `git diff --stat`, `git diff --name-status`, `git status`, `git ls-files`; оценить неотслеживаемые тесты. Особенно `.gitignore`: паттерн не должен молча скрывать `tests/test_token_store.py` и другие новые тесты.
- **Безопасность и приватность.** Никаких токенов, cookies, access URLs и персональной истории, заимствованных музыкальных файлов/закрытых исходников.
- **Лицензии.** Указать все зависимости, изменения схемы и условия распространения. Не возвращать GPL-Mutagen в стандартный Python runtime.
- **Откат.** Одна фраза, как безопасно revert без потери данных, миграций или записей пользователей.

## 2. Что именно блокирует merge

1. Падающий обязательный CI или CI без доказательства необходимых tests (если CI не запускался, не сливать как «зелёный»).
2. Большой рефакторинг с изменением публичных CLI flags, JSON structure, error types или domain Track без baseline/regression теста.
3. Неоправданное вмешательство в AudioBackend/mpv IPC с риском deadlock, старых EOF событий, клиппинга >100%, без живых mpv тестов.
4. **Vendor-specific детали Yandex за пределами адаптера**, или прямой вызов streaming API из UI; разрушение `provider:track_id`.
5. Секреты/приватный контент в тестовом файле или логе. Нет автоматического stream ripping/DRM bypass.
6. Скрытая новая зависимость PySide6/dbus/LLM в импорте CLI/core, приводящая к невозможности `neurowave --help` без полного окружения.
7. Новый код без понятного maintainer: PR нельзя объяснить по компонентам; агент утверждает «всё проверено», не приводя реально выполненных команд.
8. Любое опасное файловое действие (USB/кеш/хранилище токенов) без fail-closed проверки путей, согласия и негативных тестов.

## 3. Принцип малых PR

- По умолчанию до **300–500 изменённых строк логики**; исключение — механическое перемещение кода с минимальным функциональным diff и контрактными тестами.
- Один PR на один тип ответственности: нельзя одновременно переписать `cli.py`, Yandex adapter, Qt и LICENSE.
- Не «написать весь роадмап» в одном PR. Прежде чем добавлять функции, проверить backlog из [STABILIZATION-PLAN](STABILIZATION-PLAN-2026-11-17.md).
- Для невоспроизводимого внешнего сбоя принять минимальный PoC/diagnostic вместо мнимого исправления.

## 4. Предлагаемые команды перед публикацией

```bash
git status --short
git diff --check
git diff --name-status origin/main...HEAD
git diff --stat origin/main...HEAD
git ls-files tests

python -m compileall -q src tests
python -m pytest -q -m "not integration" --ignore=tests/test_ui_qt.py
QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_ui_qt.py
python -m pytest -q -m integration
python -m build
```

Если нет Qt/mpv/build, прямо написать `NOT RUN: unavailable dependency`. Реальные устройства и аккаунт проверяются только в отдельном ручном протоколе, без секретов в PR.

## 5. Дополнительные gates по классам изменений

| Класс PR | Обязательное |
|---|---|
| Docs-only | относительные ссылки рабочие; нет заявлений о несуществующих функциях; stale status не возвращается |
| Provider/API | fake/contract fixtures, 401/403/429/timeout/неизвестная schema, no secrets/network in CI |
| CLI refactor | snapshot `--help`, JSON keys, exit codes, no mandatory Qt/import side effects |
| mpv/DSP | real mpv integration, rollback, true-peak safety, EOF generation, CPU/PSS |
| GUI | Qt offscreen + manual Mint smoke, main-thread responsive, IPC thread safety |
| Security/USB | explicit confirmation, validated target, symlink/race/ENOSPC/unplug tests |
| Packaging | LICENSE, third-party notices, wheel/sdist inspection, `.gitignore` audit, clean-install test |

## 6. Готово к merge, если

- [ ] scope и контракт понятны человеку;
- [ ] tests действительно запущены, CI зеленый;
- [ ] дифф и скрытые gitignore файлы проверены;
- [ ] нет регрессии качества звука и сторонней лицензии;
- [ ] документация соответствует коду, открытые проверки честно отмечены;
- [ ] rollback описан;
- [ ] отдельный maintainer **осознанно** одобрил изменения.

Ссылка на индивидуальные наряды агенту: [AGENT-WORK-ORDERS](AGENT-WORK-ORDERS-2026-11-17.md).
