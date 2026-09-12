# Agent Testbench

Тестовая папка для проверки агентов и оркестратора на малых базах данных, собранных из исходных файлов хакатона.

## Что внутри

- `agents/` - пустые Python-файлы для будущей реализации агентов и оркестратора.
- `databases/csv/` - тестовые таблицы в CSV.
- `databases/json/process_state_snapshots.jsonl` - готовые снапшоты `ProcessState` для запуска агентов.
- `databases/oilcode_agent_test.db` - SQLite-версия тех же тестовых данных.
- `docs/agents.md` - контракт работы каждого агента: вход, анализ, выход.
- `docs/database.md` - описание тестовых баз и связи с исходными данными хакатона.
- `scripts/build_test_databases.py` - генератор тестовых CSV, JSONL и SQLite.
- `app.py` и `public/` - примитивный интерфейс для запуска оркестратора и просмотра agent trace.

## Исходная логика

Стенд следует архитектуре из обсуждения:

```text
Источники
-> загрузка и очистка
-> синхронизация по времени
-> ProcessState / Blackboard
-> DQ Agent
-> Quality Agent + Reliability Agent
-> Scenario Agent
-> Safety Gate
-> Ranking / Orchestrator
-> Recommendation Packet
```

Главный принцип данных:

```text
ЛИМС -> ПАК -> ВАК -> КИП
```

ЛИМС считается контрольным фактом качества, ПАК даёт оперативное качество, ВАК даёт расчётную оценку, а КИП описывает технологический режим.

## Как пересобрать тестовые базы

Запускать из корня репозитория:

```bash
/Users/vozderjus/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 "Code 3.0/agent_testbench/scripts/build_test_databases.py"
```

Скрипт не изменяет исходные файлы хакатона. Он только читает их и перезаписывает тестовые файлы внутри `Code 3.0/agent_testbench/databases/`.

## Как запустить интерфейс

Из корня репозитория:

```bash
/Users/vozderjus/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 "Code 3.0/agent_testbench/app.py"
```

После запуска открыть:

```text
http://127.0.0.1:8765
```

Интерфейс показывает:

- список тестовых таблиц;
- выбранный `ProcessState`;
- вход каждого агента;
- шаги анализа внутри агента;
- структурированный выход;
- итоговый `RecommendationPacket` и сообщение оператору.
