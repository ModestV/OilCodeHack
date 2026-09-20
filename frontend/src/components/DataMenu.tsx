import { useRef, useState } from "react";
import { ChevronDown, Database, Upload } from "lucide-react";
import type { Manifest } from "../types";
import { HelpTooltip } from "./HelpTooltip";
import { Popover } from "../ui/Popover";
import { Select } from "../ui/Select";
import { ThemeSwitch } from "../ui/ThemeSwitch";

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
  showTheme = false,
}: {
  datasets: Manifest[];
  datasetId: string;
  setDatasetId: (id: string) => void;
  onUpload: () => void;
  showTheme?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const active = datasets.find((dataset) => dataset.id === datasetId);

  return (
    <div className="data-menu">
      <button
        ref={trigger}
        type="button"
        className="data-menu-trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Database aria-hidden="true" />
        <span>
          <small>Данные</small>
          {active?.name || "Набор не выбран"}
        </span>
        <ChevronDown aria-hidden="true" />
      </button>
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={trigger}
        placement="bottom-end"
        label="Данные"
        className="data-menu-popover"
        sheetTitle="Данные"
      >
        <div className="popover-heading">
          <span>Исторический набор</span>
          <HelpTooltip label="Набор данных">
            Сервис работает с загруженной исторической выгрузкой. Данные не
            поступают в реальном времени.
          </HelpTooltip>
        </div>
        <label htmlFor="dataset">Активный набор</label>
        <Select
          id="dataset"
          label="Активный набор"
          block
          value={datasetId}
          onChange={(id) => setDatasetId(id)}
          options={datasets.map((dataset) => ({
            value: dataset.id,
            label: dataset.name,
            description: statusName[dataset.status],
          }))}
        />
        <button
          type="button"
          className="secondary data-upload"
          onClick={() => {
            setOpen(false);
            onUpload();
          }}
        >
          <Upload aria-hidden="true" /> Загрузить данные
        </button>
        {showTheme && (
          <div className="popover-row">
            <span>Тема</span>
            <ThemeSwitch />
          </div>
        )}
      </Popover>
    </div>
  );
}
