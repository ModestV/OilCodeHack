#!/bin/sh
# Import the organisers' data once, then serve UI + API on :8000.
set -eu

DATASET_ID="${OILCODE_DATASET_ID:-hackathon}"
DATA_DIR="${OILCODE_DATA_DIR:-/data}"

if [ ! -f "$OILCODE_STORAGE/$DATASET_ID/manifest.json" ] && [ -d "$DATA_DIR" ]; then
  SOURCE="$DATA_DIR"
  ARCHIVES=$(find "$DATA_DIR" -maxdepth 3 -type f \( -iname '*.rar' -o -iname '*.zip' -o -iname '*.7z' \) | head -n 5)
  if [ -n "$ARCHIVES" ]; then
    # The mounted folder stays read-only: copy tables and unpack archives to a temp dir.
    SOURCE=$(mktemp -d)
    find "$DATA_DIR" -type f \( -iname '*.csv' -o -iname '*.xlsx' \) ! -name '~$*' -exec cp {} "$SOURCE"/ \;
    echo "$ARCHIVES" | while read -r archive; do
      echo "Распаковка $archive"
      bsdtar -xf "$archive" -C "$SOURCE"
    done
  fi
  if find "$SOURCE" -type f \( -iname '*.csv' -o -iname '*.xlsx' \) | grep -q .; then
    echo "Импорт данных из $DATA_DIR в набор $DATASET_ID (один раз, несколько минут)…"
    python scripts/import_demo.py "$SOURCE" --id "$DATASET_ID"
  else
    echo "В $DATA_DIR нет CSV/XLSX: загрузите файлы через интерфейс."
  fi
fi

exec python -m uvicorn backend.app:app --host 0.0.0.0 --port "${PORT:-8000}"
