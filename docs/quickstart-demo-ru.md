# Быстрый запуск демонстрации

Эта инструкция предназначена для проверки за несколько минут. Ветка `demo`
содержит уже нормализованные данные в Git LFS: импортировать CSV/XLSX или
создавать базу данных вручную не нужно.

## Требования

- Git с Git LFS;
- Python 3.11–3.13;
- Node.js 22.12+ и npm.

## Запуск

```powershell
git clone --branch demo https://github.com/ModestV/OilCodeHack.git
cd OilCodeHack
git lfs pull
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm --prefix frontend ci
.venv\Scripts\python.exe scripts/run_demo.py
```

После запуска открыть [http://127.0.0.1:5173](http://127.0.0.1:5173).
Скрипт запускает API и интерфейс вместе; остановка — `Ctrl+C`.

Если команда `py` отсутствует, заменить её на `python`. На Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
npm --prefix frontend ci
.venv/bin/python scripts/run_demo.py
```

## Проверка без интерфейса

В отдельном окне после запуска:

```powershell
.venv\Scripts\python.exe scripts/verify_release.py --local --dataset hackathon --output demo-acceptance.json
```

Ожидаемый результат: 8 из 8 сценариев `PASS`.

## Важные даты для демонстрации

| Дата | Что показать |
|---|---|
| `2026-01-08 07:00` | обычный исторический срез и удержание режима |
| `2026-01-14 07:00` | опубликованное превышение серы и отказ |
| `2026-06-19 12:00` | отказ по области применимости прогноза |
| `2026-08-07 00:00` | диагностика устаревшего цетана и отказ рекомендации |

## Что лежит в данных

```text
storage/hackathon/
├── manifest.json             паспорт и список источников
└── observations.parquet      18,6 млн нормализованных наблюдений
```

Parquet-файл хранит только нормализованный набор, а не отдельную СУБД. API
читает его локально через DuckDB. Файлы исходной загрузки в демо-ветку не
добавлялись, потому что они не нужны для запуска и увеличили бы архив.

## Если Git LFS недоступен

Можно запустить `main` без встроенных данных: загрузить исходные CSV/XLSX
через интерфейс или выполнить `scripts/import_demo.py` с локальным каталогом.
Это запасной путь; для быстрой оценки рекомендуется ветка `demo`.
