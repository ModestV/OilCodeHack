import { useEffect, useRef, useState } from "react";
import {
  Upload,
  X,
  FileSpreadsheet,
  CheckCircle2,
  AlertCircle,
} from "lucide-react";
import { api } from "../api";
import type { Manifest } from "../types";
export function UploadModal({
  open,
  onClose,
  onStarted,
}: {
  open: boolean;
  onClose: () => void;
  onStarted: (m: Manifest) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const dialog = useRef<HTMLElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement;
    setError("");
    dialog.current?.querySelector("input")?.focus();
    return () => previous?.focus();
  }, [open]);
  if (!open) return null;
  async function submit() {
    if (!files.length) return;
    setBusy(true);
    setError("");
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    if (name.trim()) form.append("name", name.trim());
    try {
      const m = await api.upload(form);
      setFiles([]);
      setName("");
      onStarted(m);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось загрузить файлы");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="modal-backdrop" role="presentation">
      <section
        ref={dialog}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="upload-title"
        onKeyDown={(e) => {
          if (e.key === "Escape" && !busy) onClose();
          if (e.key === "Tab") {
            const items = dialog.current?.querySelectorAll<HTMLElement>(
              "button:not(:disabled),input:not([hidden])",
            );
            if (!items?.length) return;
            const first = items[0],
              last = items[items.length - 1];
            if (e.shiftKey && document.activeElement === first) {
              e.preventDefault();
              last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
              e.preventDefault();
              first.focus();
            }
          }
        }}
      >
        <header>
          <div>
            <h2 id="upload-title">Загрузить исторические данные</h2>
            <p>
              Добавьте доступные файлы КИП, ЛИМС или ПАК. Неполный комплект
              допустим.
            </p>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Закрыть">
            <X />
          </button>
        </header>
        <label className="field">
          <span>Название набора</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Например, июльская выгрузка"
          />
        </label>
        <button className="dropzone" onClick={() => input.current?.click()}>
          <Upload />
          <strong>Выберите файлы</strong>
          <span>CSV, XLSX — можно несколько</span>
        </button>
        <input
          ref={input}
          hidden
          multiple
          type="file"
          accept=".csv,.xlsx"
          onChange={(e) => setFiles(Array.from(e.target.files || []))}
        />
        <div className="file-list">
          {files.map((f, i) => (
            <div key={`${f.name}-${i}`}>
              <FileSpreadsheet />
              <span>
                {f.name}
                <small>{(f.size / 1024 / 1024).toFixed(1)} МБ</small>
              </span>
              <button
                className="icon-btn"
                onClick={() => setFiles((v) => v.filter((_, j) => j !== i))}
              >
                <X />
              </button>
            </div>
          ))}
        </div>
        {error ? (
          <p className="error">
            <AlertCircle /> {error}
          </p>
        ) : null}
        {busy ? (
          <div className="progress">
            <span />
            <p>Файлы загружены, начинается обработка…</p>
          </div>
        ) : null}
        <footer>
          <button className="secondary" onClick={onClose}>
            Отмена
          </button>
          <button
            className="primary"
            disabled={!files.length || busy}
            onClick={submit}
          >
            {busy ? (
              "Загрузка…"
            ) : (
              <>
                <CheckCircle2 /> Начать импорт
              </>
            )}
          </button>
        </footer>
      </section>
    </div>
  );
}
