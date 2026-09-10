import { useDeferredValue, useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Plus, Search, X } from "lucide-react";
import type { Metric } from "../types";
export function MetricPicker({
  open,
  onClose,
  metrics,
  pinned,
  setPinned,
}: {
  open: boolean;
  onClose: () => void;
  metrics: Metric[];
  pinned: string[];
  setPinned: (p: string[]) => void;
}) {
  const [q, setQ] = useState("");
  const search = useDeferredValue(q.toLowerCase());
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement;
    dialog.current?.querySelector("input")?.focus();
    return () => previous?.focus();
  }, [open]);
  if (!open) return null;
  const rows = metrics
    .filter((m) =>
      `${m.label} ${m.id} ${m.group}`.toLowerCase().includes(search),
    )
    .sort((a, b) => {
      const ai = pinned.indexOf(a.id),
        bi = pinned.indexOf(b.id);
      return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi);
    });
  function move(i: number, d: number) {
    const n = [...pinned];
    [n[i + d], n[i]] = [n[i], n[i + d]];
    setPinned(n);
  }
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        ref={dialog}
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label="Показатели"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Escape") onClose();
          if (e.key === "Tab") {
            const nodes = dialog.current?.querySelectorAll<HTMLElement>(
              "button:not(:disabled),input",
            );
            if (!nodes?.length) return;
            const first = nodes[0],
              last = nodes[nodes.length - 1];
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
          <h2>
            Показатели <small>{pinned.length}</small>
          </h2>
          <button
            className="icon-btn"
            onClick={onClose}
            aria-label="Закрыть показатели"
          >
            <X />
          </button>
        </header>
        <label className="search">
          <Search />
          <input
            aria-label="Поиск показателя"
            placeholder="Название, тег или группа"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </label>
        <div className="metric-list">
          {rows.map((m) => {
            const i = pinned.indexOf(m.id);
            return (
              <div key={m.id}>
                <span>
                  <strong>{m.label}</strong>
                  <small>
                    {m.id} · {m.source.toUpperCase()}
                  </small>
                </span>
                {i >= 0 ? (
                  <>
                    <button
                      disabled={i === 0}
                      aria-label={`Выше: ${m.label}`}
                      title="Выше"
                      onClick={() => move(i, -1)}
                    >
                      <ArrowUp />
                    </button>
                    <button
                      disabled={i === pinned.length - 1}
                      aria-label={`Ниже: ${m.label}`}
                      title="Ниже"
                      onClick={() => move(i, 1)}
                    >
                      <ArrowDown />
                    </button>
                    <button
                      aria-label={`Убрать: ${m.label}`}
                      title="Убрать"
                      onClick={() =>
                        setPinned(pinned.filter((x) => x !== m.id))
                      }
                    >
                      <X />
                    </button>
                  </>
                ) : (
                  <button
                    aria-label={`Добавить: ${m.label}`}
                    title="Добавить"
                    onClick={() => setPinned([...pinned, m.id])}
                  >
                    <Plus />
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
