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

## Docker и закрытая сеть

`docker compose up --build` собирает один образ (интерфейс + API) и при первом старте импортирует
`./data`. Для машины без интернета образы и модель переносятся заранее:

```bash
# на машине с интернетом
docker compose -f docker-compose.yml -f docker-compose.llm.yml build
docker compose -f docker-compose.yml -f docker-compose.llm.yml run --rm ollama-pull
docker save oilcode:latest ollama/ollama:latest -o oilcode-images.tar
docker run --rm -v oilcode_ollama:/m -v "$PWD":/out alpine tar -cf /out/ollama-models.tar -C /m .
# в закрытом контуре
docker load -i oilcode-images.tar
docker volume create oilcode_ollama
docker run --rm -v oilcode_ollama:/m -v "$PWD":/in alpine tar -xf /in/ollama-models.tar -C /m
docker compose -f docker-compose.yml -f docker-compose.llm.yml up --no-build
```

Если в контуре уже есть OpenAI-совместимая LLM, Ollama не нужна: задайте `OILCODE_LLM_BASE_URL`
и `OILCODE_LLM_MODEL` для сервиса `app` и запускайте только `docker compose up`.

Журнал решений пишется в `storage/<id>/decisions` (~200 КБ на решение); `OILCODE_DECISION_LOG=0`
отключает запись.

## Ограничения

Сервер не получает исходные большие таблицы автоматически. Их нужно импортировать через UI/API после первого развёртывания или заранее положить в `storage/`. Внешний LLM API в production-контур не требуется.
