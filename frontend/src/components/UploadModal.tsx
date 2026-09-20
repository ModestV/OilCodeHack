import { useRef, useState, type DragEvent } from "react";
import {
  Upload,
  X,
  FileSpreadsheet,
  CheckCircle2,
  AlertCircle,
} from "lucide-react";
import { api } from "../api";
import type { Manifest } from "../types";
import { Dialog } from "../ui/Dialog";
import { TextField } from "../ui/Controls";

const ACCEPT = /\.(csv|xlsx)$/i;
const MAX_FILES = 8;
const MAX_TOTAL = 800 * 1024 * 1024;

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
  const [files, setFiles] = useState<File[]>([]);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);

  const add = (incoming: File[]) => {
    const accepted = incoming.filter(
      (f) => ACCEPT.test(f.name) && !f.name.startsWith("~$"),
    );
    const rejected = incoming.length - accepted.length;
    const next = [...files, ...accepted].slice(0, MAX_FILES);
    const total = next.reduce((sum, f) => sum + f.size, 0);
    setError(
      rejected
        ? "Принимаются только CSV и XLSX; временные файлы ~$… пропущены."
        : total > MAX_TOTAL
          ? "Суммарный размер файлов превышает 800 МБ."
          : files.length + accepted.length > MAX_FILES
            ? `Не более ${MAX_FILES} файлов на один набор.`
            : "",
    );
    setFiles(next);
  };
  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    add(Array.from(event.dataTransfer.files));
  };

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

  const close = () => {
    if (!busy) onClose();
  };

  return (
    <Dialog
      open={open}
      onClose={close}
      title="Загрузить исторические данные"
      description="Добавьте доступные файлы КИП, ЛИМС или ПАК. Неполный комплект допустим."
      width={560}
      footer={
        <>
          <button
            type="button"
            className="secondary"
            onClick={close}
            disabled={busy}
          >
            Отмена
          </button>
          <button
            type="button"
            className="primary"
            disabled={!files.length || busy}
            onClick={submit}
          >
            {busy ? (
              "Загрузка…"
            ) : (
              <>
                <CheckCircle2 aria-hidden="true" /> Начать импорт
              </>
            )}
          </button>
        </>
      }
    >
      <TextField
        label="Название набора"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Например, июльская выгрузка"
        autoComplete="off"
      />
      <button
        type="button"
        className={`dropzone${dragging ? " dragging" : ""}`}
        onClick={() => input.current?.click()}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <Upload aria-hidden="true" />
        <strong>Выберите файлы или перетащите сюда</strong>
        <span>CSV, XLSX — до {MAX_FILES} файлов и 800 МБ</span>
      </button>
      <input
        ref={input}
        hidden
        multiple
        type="file"
        accept=".csv,.xlsx"
        onChange={(e) => {
          add(Array.from(e.target.files || []));
          e.target.value = "";
        }}
      />
      {files.length > 0 && (
        <ul className="file-list">
          {files.map((f, i) => (
            <li key={`${f.name}-${i}`}>
              <FileSpreadsheet aria-hidden="true" />
              <span>
                {f.name}
                <small>{(f.size / 1024 / 1024).toFixed(1)} МБ</small>
              </span>
              <button
                type="button"
                className="icon-button"
                aria-label={`Убрать ${f.name}`}
                onClick={() => setFiles((v) => v.filter((_, j) => j !== i))}
              >
                <X />
              </button>
            </li>
          ))}
        </ul>
      )}
      {error ? (
        <p className="error" role="alert">
          <AlertCircle aria-hidden="true" /> {error}
        </p>
      ) : null}
      {busy ? (
        <div className="progress" role="status">
          <span />
          <p>Файлы загружены, начинается обработка…</p>
        </div>
      ) : null}
    </Dialog>
  );
}
