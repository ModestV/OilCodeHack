# Развёртывание

Приложение работает в закрытом локальном контуре: FastAPI обслуживает собранный React из `frontend/dist`, а исходные загрузки остаются в каталоге `storage/` и не попадают в Git.

## Ручной запуск на сервере

На машине, где собран `frontend/dist`, скопировать содержимое репозитория в `/home/<user>/oilcode`, затем на сервере выполнить:

```bash
cd /home/<user>/oilcode
export OILCODE_APP_ROOT=/home/<user>/oilcode
export OILCODE_PORT=8000
export OILCODE_USER=<user>
bash deploy/server/remote_deploy.sh
```

Скрипт создаёт виртуальное окружение, устанавливает `requirements.txt`, регистрирует systemd unit, включает nginx при его наличии и проверяет `/api/health`. Данные в `storage/` не удаляются.

## Автодеплой

`.github/workflows/deploy.yml` запускается при push в `main` и вручную через `workflow_dispatch`. В настройках GitHub Actions нужны secrets:

- `DEPLOY_HOST` — IP или DNS сервера;
- `DEPLOY_USER` — пользователь Linux;
- `DEPLOY_PATH` — например `/home/romakrutoi/oilcode`;
- `DEPLOY_SSH_KEY` — закрытый ключ для этого пользователя.

Workflow сначала выполняет backend/model tests и production build фронтенда. Только после этого копирует release через SSH/rsync, перезапускает сервис и проверяет health endpoint.

## Ограничения

Сервер не получает исходные большие таблицы автоматически. Их нужно импортировать через UI/API после первого развёртывания или заранее положить в `storage/`. Внешний LLM API в production-контур не требуется.
