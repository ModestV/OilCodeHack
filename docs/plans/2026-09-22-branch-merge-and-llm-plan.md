# План: слияние веток, недостающие функции, LLM и запуск для судей — 22.09.2026

База сравнения: `origin/main@bbb6a34`. Локальный `main` отстаёт на 9 коммитов
(`d8afb0b`). Перед работой: `git switch main && git pull --ff-only`.

Главное правило плана — **переносить функции, а не сливать ветки**. Модельные
ветки созданы от старой базы и конфликтуют с `main` в 20+ файлах (`agents.py`,
`scenarios.py`, `forecast.py`, `objectives.py`, тесты). В `ac0fa66` команда уже
отклонила полное слияние v4 из-за временных утечек и потери модели партии,
запаса и транспорта. Поэтому каждая функция переносится отдельным PR и
адаптируется к контракту `main`.

## 1. Инвентаризация веток

### Уже в `main`: удалить после подтверждения

| Ветка | Состояние |
|---|---|
| `codex/frontend`, `frontend-monitoring` | 0 коммитов впереди |
| `Human-DataMap` | 0 впереди; `DataMap/` уже в main. **Не сливать** (правило команды) |
| `codex/first-iteration-integrated`, `codex/model-improvement`, `codex/process-corrections`, `codex/integrated-anchored-forecast`, `codex/cost-sensitive-risk`, `codex/cetane-t95-audit`, `codex/quality-diagnostics`, `codex/review-model-v4` | 0 впереди, вошли через `ac0fa66` |
| `codex/causal-anchored-forecast` (`852af10`) | формально 2 впереди, но все файлы (`anchored_*`, `calibration_memory`, `replay_anchored`) уже в main в более новой версии. Поглощена |

### Не подлежат слиянию

| Ветка | Почему | Что взять |
|---|---|---|
| `Human-DataMap` | правило команды; Obsidian-карта | ничего, уже в main |
| `case_info` (сирота) | архив материалов, `data.rar` 100 МБ с данными организаторов | тексты `requirements.md`, `organizer_clarifications.md` → `docs/case/` (без `materials/`) |
| `sholtiee-agents` (сирота, worktree `../Agents`) | отдельный репозиторий контекста агента | ничего |
| `agents_test` (vozderjus) | параллельный прототип на синтетике: закоммичены `.pyc` и sqlite, устаревший тег `T5` вместо `T6` | матрицу стресс-сценариев переписать как тесты на реальный `backend/agents.py` (см. 2.4) |
| `architecture-docs` (vozderjus) | предварительная, теги устарели | схему «LLM объясняет только проверенный результат» — в новый `docs/architecture.md` |
| `fix/forecast-accuracy` | предок v4 и professional-ui, заменён main v3 | ничего напрямую; см. agent-backtest ниже |

### Содержат ценные функции: переносить выборочно

| Ветка | Лучшее | Риск |
|---|---|---|
| `feat/forecast-v4-contour-v2` (`2397769`) | блок **конфликтов и согласованности** оркестратора, шаблонное **объяснение** (`backend/explain.py`), сетка ~36 кандидатов, доверие к рекомендации, agent-backtest | модель v4 признана методологически ошибочной (утечки фолдов, `quiet_future`). **Модель не переносить** |
| `fix/forecast-v4-repair` (локально, worktree `../OilCodeHack-v4-repair`) | исправленный v4r, адаптер `backend/forecast_two_stage.py` через env, 24 теста на утечки, parity 4222/4222, протокол до расчётов | в README отчёта остался плейсхолдер `FRESHNESS_SECTION`; всё, кроме `PROTOCOL.md`, **не закоммичено** |
| `design/professional-ui` (9 коммитов) | собственный UI-kit (`ui/*`), токены и тёмная тема, mobile-first, code splitting, KPI-плитки, **URL-состояние** (глубокие ссылки на момент), разбивка `MonitoringViews` на `Overview/Trends/Statistics/Kip/DataQuality`, `docs/architecture-pipeline.svg` | внутри 3 модельных коммита `fix/forecast-accuracy`; `DecisionSupportView` расходится с main (нет `QualityDiagnosticsPanel`, Парето, v3-контракта) |
| `codex/dark-operator-ui` (3 коммита, RomashkaHH) | тёмная тема оператора, `UiSelect`, `DateTimeField`, `ContextMenu`, ленивый `ChartImpl` | 1 конфликт (`DecisionSupportView.tsx`); база близка к main |

## 2. Порядок слияния

Каждый шаг — отдельная ветка от свежего `main` и PR с зелёными проверками:
`pytest`, `unittest` в `tools/modeling`, `npm test`, `npm run build`,
`scripts/verify_release.py --local`.

### 2.1 Закоммитить и влить v4r как выключенный адаптер (1–2 ч)
1. В `../OilCodeHack-v4-repair` заменить `FRESHNESS_SECTION` реальным текстом
   (или пометкой «не рассчитано»), затем закоммитить код, тесты, отчёт и `AGENT/`.
2. PR `fix/forecast-v4-repair` → main. База уже `bbb6a34`, конфликтов нет.
   По умолчанию runtime остаётся v3, v4r включается
   `OILCODE_FORECAST_MODEL=two-stage-v4r`. Это соответствует протоколу:
   v4r не продвигается.

### 2.2 Выбрать один интерфейс (решение команды, 0.5 ч + 3–5 ч)
Две ветки переписывают одни и те же компоненты, поэтому обе влить нельзя.
Рекомендация: **professional-ui как основа**. Там больше функций для судей:
мобильная вёрстка, URL-ссылки на демо-моменты, разбитые представления, тесты
токенов. Из dark-operator-ui нужны только цвета тёмной темы оператора.
1. Ветка `ui/unify` от main. `git checkout origin/design/professional-ui -- frontend/src/ui frontend/src/styles frontend/src/views/{Overview,Trends,Statistics,Kip,DataQuality,shared}.tsx frontend/src/urlState.ts frontend/src/chartTheme.ts frontend/tests docs/architecture-pipeline.svg …`
   **без** backend и `tools/modeling`.
2. Вручную перенести `DecisionSupportView.tsx` на контракт main: v3-прогноз,
   `QualityDiagnosticsPanel`, аннотация Парето, партия/запас/бленд.
   `types.ts` берётся из main.
3. Токены тёмной темы из `codex/dark-operator-ui/operator-theme.css` добавить
   в `styles/tokens.css`. Остальное в dark-operator-ui закрыть.
4. Проверить в браузере 6 демо-моментов из README на десктопе и мобильном.

Если команда выберет dark-operator-ui: она сливается почти чисто
(1 конфликт), но мобильная вёрстка, URL-состояние и KPI будут потеряны.

### 2.3 Оркестратор: конфликты, согласованность, объяснение (3–4 ч)
В ТЗ прямо названы «разрешение конфликтов целей» и «объяснение рекомендации».
Сейчас в main оркестратор только склеивает 3 результата. Из v4 переносим
**идею, а не файл**, так же как ранее переносили Парето:
- `_conflicts`: quality↔reliability, действие при низком доверии,
  неопределённый эффект, выбран заблокированный параметр. Результат —
  `abstain` или `warning`;
- `_consistency`: сумма долей бленда = 100%, выбранный кандидат прошёл
  все проверки, прогноз и рекомендация ссылаются на один момент `at`;
- `backend/explain.py`: детерминированный шаблон «что рекомендуем → почему →
  какие ограничения проверены → доверие к данным → альтернативы или причина
  отказа». Это обязательная основа, и LLM (раздел 4) её только перефразирует;
- 4-й шаг trace `orchestrator` с полями `consumes/produces`.
Тесты: для каждого вида конфликта — вход и ожидаемое `status/abstain.reason`.

### 2.4 Матрица стресс-сценариев (2 ч)
Сценарии из `agents_test/tests/test_stress_matrix.py` и `test_decision_matrix.py`
переписываются на реальные данные и теги `T6/F9/P13` в
`tests/test_decision_matrix.py`: устаревший ЛИМС + свежий ПАК, нет ПАК,
конфликт ЛИМС/ПАК, flatline F9, нет обязательного тега, нет источника
качества вообще. Демо-даты те же, что в `reports/verification/cases.json`.

### 2.5 Бэктест агентов (2 ч, по желанию)
`tools/modeling/backtest_agents.py` и отчёт `agent-backtest` из
`fix/forecast-accuracy` показывают, как часто контур рекомендует или
отказывается на истории. Сначала сравнить с `replay_decision_coverage.py`
из main и переносить только то, чего там нет. Бэктест запускается на
текущем v3.

### 2.6 Документы (1 ч)
`docs/case/` — требования и уточнения организаторов из `case_info`, только
тексты. `docs/architecture.md` — пайплайн по `architecture-docs` с
актуальными тегами и SVG из professional-ui.

### 2.7 Уборка
Удалить слитые и поглощённые удалённые ветки (список в разделе 1) только после
согласия владельцев (RomashkaHH, vozderjus). `Human-DataMap` и `case_info`
оставить как архив.

## 3. Чего не хватает проекту

| Пробел | Почему важно | Как закрыть |
|---|---|---|
| Журнал решений | ТЗ требует сохранять входы, оценки агентов и рекомендацию для аудита. Сейчас ответ только возвращается | `storage/<id>/decisions/<at>_<hash>.json` (запрос, trace, версии модели и кода, объяснение), `GET /api/datasets/{id}/decisions` |
| Режим пуска/останова | прямой бонус в уточнениях организаторов | детектор по F9 ≈ 0 или резкому падению T6 → состояние `shutdown/startup`, отказ с понятной причиной и тестом |
| Объяснение и конфликты | критерий «объяснимость» | раздел 2.3 |
| Проверки PR | `deploy.yml` запускается только на push в main | `ci.yml` на `pull_request`: те же проверки без деплоя |
| Закреплённые версии | `requirements.txt` задаёт только диапазоны | `requirements.lock` (`uv pip compile`) для Docker и CI |
| Простой запуск | сейчас 5 команд, Node 22 и ручной импорт | раздел 5 |
| Презентация | обязательна в финале | отдельная задача по README и отчётам |
| README | сплошной текст, быстрый старт теряется | раздел 5.4 |

## 4. LLM: как внедрить

Уточнения организаторов: в закрытой сети есть **локальная LLM со стандартным
API** класса `gpt-oss-20b` или Qwen ~27B, и LLM не обязана быть в каждом
агенте. Поэтому LLM — **необязательный объяснитель** поверх детерминированного
контура. Она не участвует в выборе действия и не меняет ограничения.

### Архитектура
```
Quality → Reliability → Optimization → Orchestrator ─► decision (детерминированно)
                                                   └► explain.py (шаблон, всегда)
                                                         └► LLM Explainer (если включён)
                                                               └► валидатор → текст | откат к шаблону
```
- `backend/llm.py`: клиент OpenAI-совместимого `/v1/chat/completions` на
  stdlib `urllib`, без новых зависимостей. Подходит для Ollama, vLLM,
  llama.cpp server, LM Studio и шлюза организаторов.
- Настройки из env, по умолчанию LLM **выключена**:
  `OILCODE_LLM_BASE_URL` (например `http://localhost:11434/v1`),
  `OILCODE_LLM_MODEL` (`qwen2.5:7b-instruct` / `gpt-oss:20b`),
  `OILCODE_LLM_API_KEY` (необязательно), `OILCODE_LLM_TIMEOUT_S=20`.
- Вход модели — только JSON решения (trace, safety_gate, кандидаты, прогноз,
  причины отказа, допущения) и системный промпт: «объясни на русском для
  оператора; не добавляй чисел и действий, которых нет во входе; при
  `abstain` не предлагай действий».
- **Валидатор** (ключевая часть):
  1. каждое число в ответе есть во входном JSON (с учётом округления);
  2. названное действие и параметры совпадают с `selected_candidate`;
  3. при `status=abstain` нет глаголов рекомендации;
  4. длина ограничена.
  Если хотя бы одна проверка не пройдена, отдаётся шаблон и
  `explanation.source="template"` с причиной отката.
- В ответе `/decision` поле
  `explanation: {text, source: "template"|"llm", model, latency_ms, validation}`.
  Статус, кандидаты и рекомендация **идентичны** при включённой и выключенной
  LLM. Это закрепляется тестом.
- Журнал решений (раздел 3) сохраняет промпт, модель и итог валидации.
- Фаза 2, по желанию: `POST /api/datasets/{id}/ask` — вопрос оператора
  («почему не подняли T6?») с tool calling только к read-only функциям
  (snapshot, forecast, quality-diagnostics, последнее решение). Если
  понадобится, эти функции можно отдать как MCP-сервер.
- Фронтенд — минимальная правка: в блоке рекомендации текст объяснения и
  бейдж «LLM · <модель>» или «шаблон».

### Тесты
Фейковый OpenAI-совместимый сервер в pytest проверяет:
- откат при таймауте, 5xx и невалидном JSON;
- отклонение ответа с «выдуманным» числом;
- отклонение совета при `abstain`;
- побайтовую идентичность решения с LLM и без неё.

### Честность документации
В README LLM описывается как «необязательный локальный объяснитель;
решение детерминированное». Строку «LLM-слой запланирован, но не реализован»
заменить только после влития кода.

## 5. Запуск для судей

### 5.1 Три пути в начале README
1. **Без установки:** https://oil-code.ru. Готовые ссылки на 6 демо-моментов
   работают благодаря URL-состоянию из professional-ui.
2. **Docker, одна команда:**
   ```bash
   docker compose up --build            # приложение на :8000
   docker compose --profile llm up      # + Ollama и объяснения LLM
   ```
3. **Без Docker:** `python scripts/start.py --data /путь/к/Нефтекод_2.0`,
   одинаково на Windows, macOS и Linux.

### 5.2 Docker
- `Dockerfile`, многоэтапный: `node:22-alpine` собирает `frontend/dist`,
  `python:3.12-slim` ставит `requirements.lock`, `uvicorn backend.app:app`.
  Node на машине судьи не нужен.
- `docker-compose.yml`:
  - `app`: порт 8000, том `./storage`, том `./data:/data:ro`. Entrypoint:
    если `storage/hackathon` нет, а `/data` не пуст, запускается
    `import_demo.py /data`;
  - `ollama` (profile `llm`): `ollama/ollama`, том с моделями;
  - `ollama-pull` (profile `llm`): одноразово загружает
    `${OILCODE_LLM_MODEL:-qwen2.5:3b-instruct}`. Небольшая модель по
    умолчанию работает на ноутбуке без GPU; для `gpt-oss:20b` нужно
    ~16 ГБ памяти;
  - `app` получает `OILCODE_LLM_BASE_URL=http://ollama:11434/v1`, только
    если включён profile.
- `healthcheck` на `/api/health`.
- Закрытая сеть: `docker save` / `docker load` образов и копирование каталога
  моделей Ollama. Описать в `docs/deployment.md`.

### 5.3 `scripts/start.py`, кроссплатформенный
Скрипт создаёт `.venv`, ставит зависимости, берёт готовый `frontend/dist`,
если Node нет (артефакт GitHub Release, собирается в CI), импортирует
`--data`, если набор ещё не импортирован, запускает uvicorn и печатает URL.
При `--llm URL MODEL` выставляет env. Существующий `scripts/dev.py` остаётся
для разработки.

### 5.4 Структура README
1. Что это (3 строки) и скриншот.
2. Быстрый старт (5.1).
3. Где взять данные: комплект организаторов «Нефтекод 2.0», распакованный
   `data.rar`. Данные в репозиторий не кладём (конфиденциальность и размер).
4. Демо-сценарии: таблица «дата → ожидаемый исход → ссылка».
5. Архитектура: SVG и роли агентов.
6. Модели и честные метрики: ссылки на отчёты.
7. Проверка: `pytest`, `verify_release.py`.
8. LLM (необязательно).
9. Ограничения.
Текущие длинные правила расчётов перенести в `docs/`.

### 5.5 Проверка «чистой машиной»
В CI добавить job: `docker compose up -d` на небольшом срезе данных
(fixture в `tests/fixtures`) → `verify_release.py --base-url http://localhost:8000`.
Это доказывает, что путь из README работает.

## 6. Сводный порядок и оценка

| # | PR | Время | Зависимости |
|---|---|---|---|
| 1 | v4r-адаптер (выключен) | 1–2 ч | — |
| 2 | Единый UI | 4–6 ч | решение команды |
| 3 | Оркестратор: конфликты, согласованность, шаблонное объяснение | 3–4 ч | — |
| 4 | Матрица стресс-сценариев | 2 ч | 3 |
| 5 | Журнал решений + пуск/останов | 3 ч | 3 |
| 6 | LLM-объяснитель + валидатор + тесты | 4–5 ч | 3, 5 |
| 7 | Docker/compose + `start.py` + lock | 3–4 ч | — (параллельно) |
| 8 | CI на PR + smoke через compose | 2 ч | 7 |
| 9 | README и `docs/case`, `docs/architecture` | 2 ч | 2, 6, 7 |
| 10 | Бэктест агентов, уборка веток | 2 ч | 3 |

Пункты 1, 3 и 7 независимы и могут идти параллельно.
