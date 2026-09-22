# Нефтекод

Мультиагентная поддержка оператора цепочки АВТ → гидроочистка → блендинг дизельного топлива.
Мониторинг КИП, ЛИМС и ПАК, прогноз серы на 0–3 часа, сценарии управления T6/F9/P13 и смешения.
Агенты качества, надёжности и оптимизации передают результаты оркестратору. Он разрешает конфликты
целей и либо выдаёт объяснённую рекомендацию, либо отказывается и называет причину.
Всё работает локально, без внешних API.

## Быстрый старт

**1. Без установки:** https://oil-code.ru — развёрнутая копия с данными хакатона.

**2. Docker (нужен только Docker):** положите комплект организаторов «Нефтекод 2.0»
(`data.rar` или распакованные CSV и XLSX ЛИМС/ПАК/тегов) в папку `data/`, затем:

```bash
docker compose up --build
```

Откройте http://localhost:8000. Первый запуск один раз импортирует данные (несколько минут);
после перезапуска импорт не повторяется. Порт меняется переменной `OILCODE_PORT`.

**3. Без Docker (Python 3.11–3.13; Node.js 22.12+ для сборки интерфейса):**

```bash
python scripts/start.py --data /путь/к/Нефтекод_2.0
```

В Windows — `py scripts\start.py --data C:\путь\к\Нефтекод_2.0`. Скрипт создаёт `.venv`, ставит
зафиксированные зависимости (`requirements.lock`), собирает интерфейс, распаковывает `data.rar`
системным `tar`, импортирует данные и открывает браузер. Повторный запуск пропускает готовые шаги.
Без `--data` данные можно загрузить в интерфейсе.

### Локальная LLM (необязательно)

Решение всегда принимают детерминированные агенты. LLM только пересказывает готовое решение
оператору, и её ответ проверяется: см. [docs/architecture.md](docs/architecture.md#llm-объяснитель).

```bash
docker compose -f docker-compose.yml -f docker-compose.llm.yml up --build
```

Этот вариант поднимает Ollama и один раз загружает `qwen2.5:3b-instruct` (~2 ГБ, работает на CPU).
Модель меняется через `OILCODE_LLM_MODEL=gpt-oss:20b` и т. п. Для уже работающего
OpenAI-совместимого сервера (vLLM, llama.cpp, шлюз закрытой сети):

```bash
python scripts/start.py --llm-url http://localhost:11434/v1 --llm-model qwen2.5:3b-instruct
```

или переменные `OILCODE_LLM_BASE_URL`, `OILCODE_LLM_MODEL`, `OILCODE_LLM_API_KEY`.
Состояние видно в `GET /api/health`. При недоступности модели показывается шаблонное объяснение.

## Демонстрационные сценарии

Выберите момент в интерфейсе (раздел рекомендаций) или вызовите
`POST /api/datasets/hackathon/decision` с `{"at": "<момент>"}`. Все сценарии проверяются
автоматически: `scripts/verify_release.py`, [reports/verification/cases.json](reports/verification/cases.json).

| Момент | Что показывает | Ожидаемый исход |
|---|---|---|
| 08.01.2026 07:00 | устойчивый период, все данные свежие | рекомендация, объяснение, альтернативы |
| 14.01.2026 07:00 | прогноз сигнализирует о превышении 10 мг/кг | отказ с причиной |
| 19.06.2026 12:00 | установка остановлена (F9 ≈ 0) | отказ: режим останова |
| 08.08.2026 12:00 | устаревшая телеметрия | отказ |
| 31.12.2022 00:00 | до начала истории | отказ, прогноз не строится |
| 08.01.2026 + `current_sulfur: 20` | недостижимая цель | кандидаты видны, ни один не рекомендован |
| 08.01.2026 + `targets.sulfur_max: 100` | попытка ослабить предел | обязательный предел 10 мг/кг не ослабляется |
| 08.01.2026 + смесь с T95 = 370 | хорошая сера не компенсирует T95 | отказ |
| 2023 год | нет ПАК плотности | остальные источники доступны |

## Архитектура

Quality → Reliability → Optimization → Orchestrator → объяснение (опционально LLM) → журнал решений.
Каждый шаг виден в `trace` ответа (`consumes`/`produces`). Конфликты целей, проверки согласованности,
режим пуска/останова и LLM описаны в [docs/architecture.md](docs/architecture.md). Правила расчётов,
прогноз и интерфейс — в [docs/calculation-rules.md](docs/calculation-rules.md); контракты API —
в [docs/contracts.md](docs/contracts.md); OpenAPI — `/docs`; постановка кейса — в [docs/case/](docs/case/README.md).

## Данные

Принимаются CSV КИП АВТ/24-2000, XLSX ЛИМС и ПАК в структуре хакатона, опционально XLSX со справочником. Можно загрузить часть источников. До 8 файлов и 800 МБ на один набор. Файлы `~$…` и архивы не принимаются: CSV уже распакованы в исходном комплекте.

`storage/<id>/raw` содержит неизменные копии загрузок, `observations.parquet` — нормализованные наблюдения, `manifest.json` — паспорт набора. `storage/` исключён из Git. Альтернативный каталог задаётся переменной `OILCODE_STORAGE`.

Ряды имеют разные часы измерений. КИП: 97 сигналов, 189 217 кадров с шагом 10 минут, 01.01.2023–07.08.2026. ЛИМС: 54 независимых ряда на 6 точках отбора. ПАК серы покрывает 2023–2026 годы; плотность ПАК начинается 05.03.2025. Справочник: [docs/data-dictionary.md](docs/data-dictionary.md).

## Модель и честные метрики

Активен калиброванный по опубликованным ЛИМС Ridge v3. На принятых пробах 2026 года MAE по горизонтам
0/1/2/3 ч — 1.175/1.280/1.389/1.452 мг/кг, покрытие на 3 ч — 236 из 253 (93.3%). Предыдущая проба даёт
MAE 1.709, recall тревоги 0.35 — для самостоятельного контроля качества этого недостаточно.
2026 год уже использовался в исследованиях и не является слепым тестом.
Подробности: [reports/modeling/causal-anchored/corrected-claude/REPORT.md](reports/modeling/causal-anchored/corrected-claude/REPORT.md).

Двухступенчатая модель v4r исправлена и сравнена по протоколу, зафиксированному до расчётов.
Правило продвижения она не выполнила, поэтому доступна только как теневой вариант
(`OILCODE_FORECAST_MODEL=two-stage-v4r`): [reports/review/v4-repair-2026-09-22/README.md](reports/review/v4-repair-2026-09-22/README.md).
Бэктест всего контура агентов на пробах 2026 года: [reports/verification/agent-backtest/REPORT.md](reports/verification/agent-backtest/REPORT.md).
Аудит веток: [reports/review/new-branches/README.md](reports/review/new-branches/README.md).

## Проверка

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m unittest discover -s tools/modeling -p 'test_*.py' -v
npm --prefix frontend test
npm --prefix frontend run build
python scripts/verify_release.py --local --dataset hackathon --output reports/verification/local.json
```

Тесты покрывают временные ряды, импорт, свежесть и отсутствие утечки из будущего, прогноз, сценарии,
матрицу деградированных входов (`tests/test_decision_matrix.py`), конфликты оркестратора, режим
останова, журнал решений и проверку LLM на фейковом сервере. CI (`.github/workflows/ci.yml`) на каждом
pull request запускает тесты и собирает Docker-образ.

### Воспроизводимая проверка релиза

```bash
python tools/modeling/sulfur_first_iteration.py --source '/путь/к/hakathon-data' --output reports/modeling/sulfur-first-iteration
python tools/modeling/verify_runtime_parity.py --source '/путь/к/hakathon-data' --dataset storage/hackathon --output reports/verification/feature-parity.json
python tools/modeling/replay_anchored.py --dataset storage/hackathon --output reports/modeling/causal-anchored
python tools/modeling/verify_anchored_integration.py --help
python scripts/verify_release.py --local --output reports/verification/local.json
python scripts/verify_release.py --base-url https://oil-code.ru --expected-revision COMMIT_SHA --output reports/verification/server.json
```

Даты и ожидаемые исходы — `reports/verification/cases.json`. Отчёты содержат запросы, прогнозы, причины отказов и trace, а не только код HTTP 200. Первые две команды воспроизводят прежний Ridge; `replay_anchored.py` проверяет активный v3. На принятых пробах 2026 года MAE активного H0/H1/H2/H3 составляет 1.175/1.280/1.389/1.452 мг/кг, покрытие H3 — 236 из 253 (93.3%). На H3 MAE предыдущей доступной пробы — 1.709, recall тревоги — 0.35; этого недостаточно для самостоятельного контроля качества. 2026 год уже использовался в исследованиях и не является новым слепым тестом. Подробности: [метрики и ограничения](reports/modeling/causal-anchored/corrected-claude/REPORT.md).

[Аудит новых веток от 22.09.2026](reports/review/new-branches/README.md) объясняет выбор v3, результаты кандидата v4 и найденные ошибки. Из v4 выборочно адаптирована аннотация Парето: только допустимые варианты с полными конечными критериями, без изменения ограничений и алгоритма выбора. Диагностика T95/цетана не заменяет лабораторные доказательства и не разрешает выпуск.

## Разработка

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
npm --prefix frontend ci
.venv/bin/python scripts/import_demo.py '/путь/к/Нефтекод_2.0'
.venv/bin/python scripts/dev.py
```

`scripts/dev.py` запускает Vite (`http://127.0.0.1:5173`) и API (`:8000`) с перезагрузкой. В Windows
используйте `.venv\Scripts\python.exe`. Повторный импорт не перезаписывает набор; для новой копии — `--id another-id`.
Хранилище задаётся `OILCODE_STORAGE`.

## Сервер и автодеплой

Публичная демонстрация: https://oil-code.ru/. Прежний HTTP-адрес по IP перенаправляет на этот домен; для POST/API-проверок используйте HTTPS напрямую, чтобы 301 не превращал запрос в GET. Workflow `.github/workflows/deploy.yml` запускается на push/merge в `main`, проверяет Python и frontend, передаёт релиз через SSH/rsync, перезапускает systemd и проверяет точный SHA через `/api/health`, следуя HTTPS-перенаправлению. Данные `storage/` сохраняются. Нужны secrets `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` (на текущем репозитории настроены). На сервере: `oilcode.service`, nginx, `/home/romakrutoi/oilcode`. Ручной путь: `deploy/server/remote_deploy.sh`. Приложение не обращается к внешним LLM/API при расчётах; для первоначальной установки зависимостей необходим интернет либо подготовленный локальный кэш пакетов.

## Ограничения

Прототип для демонстрации и рассмотрения оператором: рекомендация всегда требует проверки технологом.
Сценарная модель линейная; коэффициенты отклика на T6/F9/P13 наблюдательные, причинность и промышленная
безопасность не заявляются. Исторические min/max не выдаются за паспортные пределы. Авторизации и
многопользовательского режима нет.
