# Neyrowave × DJ Franky — план бренда и внедрения

**Текущий статус:** бренд-концепция зафиксирована, иконки/анимация ещё не сгенерированы, продукт не переименован.

## Принцип разработки

Сохранять [stabilization-first](../ROADMAP.md): брендовые документы можно сохранить сейчас, **креативные/визуальные ассеты — потом**, runtime-анимацию — только после устранения P0 багов. Создание документов не даёт зелёный свет тяжёлому UI и анализаторам.

## Этапы и PR

| PR | Задача | Definition of Done |
|---|---|---|
| BRAND-00 | Зафиксировать написание, позиционирование, маскота, стиль, ТЗ анимации | 5 brand docs + корректные ссылки, no runtime |
| BRAND-01 | Варианты статичного DJ Franky icon/hero (3 концепции) | Воспроизводимый канон, проверены 16/32/64/256 px, права и attribution |
| BRAND-02 | Экспорт иконок/контраст/упаковка | PNG 1024/512/256/32/16 + при необходимости editable SVG, отделённые licences |
| BRAND-03 | Статичная интеграция в Qt/splash/tray | Исходный mpv и headless не меняются, Qt offscreen + manual Mint |
| BRAND-04 | Motion rig, demo animation (без реального аудио) | Ясные pivots/layers, CPU/PSS/stop/idle/reduced motion |
| BRAND-05 | Опциональный изолированный per-app beat visualizer | Нет global monitor; audio remains real-time; measured latency + perf |
| BRAND-06 | Packaging + legal / name migration (если подтверждено) | Trademark/name review, compatibility aliases/migrations, no broken CLI/config |

## Что одобрено / что требует решения

**Зафиксировано владельцем:** название идеи «Neyrowave — DJ Franky», Франкенштейн в модных DJ-очках и наушниках, качающий головой в такт басу и биту.

**Ожидает визуального утверждения:** точный силуэт, степень stylization, палитра/логотип и финальное написание в GUI.

**Отдельно требует решения:** полный rename `NeuroWave` → `Neyrowave`, включая GitHub, package `neurowave`, CLI, XDG folder paths, ярлык, Python entry points, MPRIS namespace, docs и пользовательские конфиги. Не ломать обратную совместимость: выбрать публичное имя отдельно от технического и продумать aliases/deprecation.

## Минимальные команды для будущей проверки

```bash
python -m pytest -q -m "not integration" --ignore=tests/test_ui_qt.py
QT_QPA_PLATFORM=offscreen python -m pytest -q tests/test_ui_qt.py
python -m pytest -q -m integration
git diff --check
```

Оформлять PR с результатами каждой реально выполненной проверки. Static asset PR не объявляется «фича готова», а анимационный PR не объявляется «beat synced», пока реальный PCM и perceived latency не измерены.

## Финальные приёмочные критерии

- [ ] Бренд узнаваем в 16–32 px, на разных фонах и без цветового канала.
- [ ] Реальные иллюстрации/исходники созданы и права зафиксированы.
- [ ] Можно полностью отключить mascot и motion без падения музыки.
- [ ] Headless/core/CLI не импортируют GUI/motion/audio analysis.
- [ ] Уважены reduced motion и ограничения по световым вспышкам.
- [ ] Персонаж не трогает очередь, громкость или пользовательские файлы без команды.
- [ ] Реальная beat-синхронизация проверена с настоящим output и в рамках latency/perf бюджета.
- [ ] Перемены названия не ломают активные пользовательские установки.
