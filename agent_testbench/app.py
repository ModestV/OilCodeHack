from __future__ import annotations

import json
import mimetypes
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agents.orchestrator import DB_PATH, list_snapshots, run_snapshot


BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"


class AppHandler(BaseHTTPRequestHandler):
    server_version = "OilCodeAgentTestbench/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_file(PUBLIC_DIR / "index.html")
            return
        if parsed.path == "/api/snapshots":
            self._send_json({"snapshots": list_snapshots()})
            return
        if parsed.path == "/api/run":
            params = parse_qs(parsed.query)
            snapshot_id = params.get("snapshot_id", ["PS-001"])[0]
            try:
                self._send_json(run_snapshot(snapshot_id))
            except KeyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/tables":
            self._send_json({"tables": self._table_names()})
            return
        if parsed.path == "/api/table":
            params = parse_qs(parsed.query)
            table = params.get("name", [""])[0]
            if table not in self._table_names():
                self._send_json({"error": "unknown table"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json({"table": table, "rows": self._table_preview(table)})
            return
        if parsed.path.startswith("/static/"):
            rel = parsed.path.removeprefix("/static/")
            self._send_file(PUBLIC_DIR / rel)
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file() or PUBLIC_DIR not in path.resolve().parents and path.resolve() != PUBLIC_DIR:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _table_names(self) -> list[str]:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "select name from sqlite_master where type = 'table' order by name"
            ).fetchall()
        return [row[0] for row in rows]

    def _table_preview(self, table: str) -> list[dict[str, object]]:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f'select * from "{table}" limit 20').fetchall()
        return [dict(row) for row in rows]


def main() -> None:
    host = "127.0.0.1"
    port = 8765
    httpd = ThreadingHTTPServer((host, port), AppHandler)
    print(f"Agent Testbench UI: http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
