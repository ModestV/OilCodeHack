import { useEffect, useRef, useState } from "react";
import { ChevronDown, Database, Upload } from "lucide-react";
import type { Manifest } from "../types";
import { HelpTooltip } from "./HelpTooltip";

const statusName = {
  ready: "Готов",
  importing: "Обрабатывается",
  error: "Ошибка",
};

export function DataMenu({
  datasets,
  datasetId,
  setDatasetId,
  onUpload,
}: {
  datasets: Manifest[];
  datasetId: string;
  setDatasetId: (id: string) => void;
  onUpload: () => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const active = datasets.find((dataset) => dataset.id === datasetId);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  return (
    <div className="data-menu" ref={root}>
      <button
        type="button"
        className="data-menu-trigger"
        aria-label="Данные"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Database />
        <span>
          <small>Данные</small>
          {active?.name || "Набор не выбран"}
        </span>
        <ChevronDown />
      </button>
      {open && (
        <div className="data-menu-popover" role="dialog" aria-label="Данные">
          <div className="popover-heading">
            <span>Исторический набор</span>
            <HelpTooltip label="Набор данных">
              Сервис работает с загруженной исторической выгрузкой. Данные не
              поступают в реальном времени.
            </HelpTooltip>
          </div>
          <label htmlFor="dataset">Активный набор</label>
          <select
            id="dataset"
            value={datasetId}
            onChange={(event) => setDatasetId(event.target.value)}
          >
            {datasets.map((dataset) => (
              <option key={dataset.id} value={dataset.id}>
                {dataset.name} · {statusName[dataset.status]}
              </option>
            ))}
          </select>
          <button
            className="secondary data-upload"
            onClick={() => {
              setOpen(false);
              onUpload();
            }}
          >
            <Upload /> Загрузить данные
          </button>
        </div>
      )}
    </div>
  );
}
