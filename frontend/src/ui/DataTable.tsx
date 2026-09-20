import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Columns3 } from "lucide-react";
import { useViewport } from "./hooks";
import { Menu, MenuItem } from "./Menu";
import { Disclosure } from "./Controls";
import "./datatable.css";

export interface Column<T> {
  key: string;
  header: ReactNode;
  /** Plain-text header for the columns menu and card labels. */
  title?: string;
  cell: (row: T) => ReactNode;
  /** primary: card title on phones; secondary: shown on the card; detail: under "Подробнее". */
  role?: "primary" | "secondary" | "detail";
  align?: "left" | "right";
  /** Hidden until enabled from the columns menu. */
  optional?: boolean;
  /** Never hideable (primary column). */
  fixed?: boolean;
  className?: string;
}

export interface DataTableProps<T> {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T) => string;
  rowClassName?: (row: T) => string | undefined;
  emptyText?: string;
  /** Enables the "Столбцы" menu and persists the choice under this key. */
  storageKey?: string;
  /** Extra toolbar content rendered next to the columns menu. */
  toolbar?: ReactNode;
  /** Table min-width on wide screens (px). */
  minWidth?: number;
  maxHeight?: number;
  /** Keep the first column visible while scrolling horizontally. */
  stickyFirst?: boolean;
  /** Force the card layout regardless of viewport. */
  cards?: boolean;
  className?: string;
  label?: string;
}

function readHidden(key: string | undefined, fallback: string[]) {
  if (!key) return fallback;
  try {
    const raw = localStorage.getItem(`oilcode:columns:${key}`);
    const parsed = raw ? JSON.parse(raw) : null;
    return Array.isArray(parsed) ? (parsed as string[]) : fallback;
  } catch {
    return fallback;
  }
}

/**
 * Table on tablet/desktop, card list on phones. Columns can be hidden through
 * a menu; the choice is remembered per storageKey.
 */
export function DataTable<T>({
  rows,
  columns,
  rowKey,
  rowClassName,
  emptyText = "Нет данных",
  storageKey,
  toolbar,
  minWidth,
  maxHeight,
  stickyFirst,
  cards,
  className = "",
  label,
}: DataTableProps<T>) {
  const viewport = useViewport();
  const defaultsHidden = useMemo(
    () => columns.filter((c) => c.optional).map((c) => c.key),
    [columns],
  );
  const [hidden, setHidden] = useState<string[]>(() =>
    readHidden(storageKey, defaultsHidden),
  );
  useEffect(() => {
    if (!storageKey) return;
    try {
      localStorage.setItem(`oilcode:columns:${storageKey}`, JSON.stringify(hidden));
    } catch {
      /* storage unavailable */
    }
  }, [hidden, storageKey]);
  const visible = columns.filter((c) => c.fixed || !hidden.includes(c.key));
  const asCards = cards ?? viewport === "phone";

  const menu =
    storageKey || toolbar ? (
      <div className="datatable-toolbar">
        {toolbar}
        {storageKey && (
          <Menu label="Столбцы" icon={<Columns3 aria-hidden="true" />} sheetTitle="Столбцы">
            {columns
              .filter((c) => !c.fixed)
              .map((c) => (
                <MenuItem
                  key={c.key}
                  keepOpen
                  selected={!hidden.includes(c.key)}
                  onSelect={() =>
                    setHidden((h) =>
                      h.includes(c.key) ? h.filter((k) => k !== c.key) : [...h, c.key],
                    )
                  }
                >
                  {c.title ?? c.header}
                </MenuItem>
              ))}
            <MenuItem onSelect={() => setHidden(defaultsHidden)}>По умолчанию</MenuItem>
          </Menu>
        )}
      </div>
    ) : null;

  if (!rows.length)
    return (
      <div className={`datatable ${className}`}>
        {menu}
        <p className="datatable-empty">{emptyText}</p>
      </div>
    );

  if (asCards) {
    const primary = visible.find((c) => c.role === "primary") ?? visible[0];
    const secondary = visible.filter(
      (c) => c !== primary && (c.role ?? "secondary") === "secondary",
    );
    const detail = visible.filter((c) => c !== primary && c.role === "detail");
    return (
      <div className={`datatable cards ${className}`} aria-label={label}>
        {menu}
        <ul className="card-list">
          {rows.map((row) => (
            <li key={rowKey(row)} className={`card-row ${rowClassName?.(row) ?? ""}`}>
              <div className="card-row-title">{primary.cell(row)}</div>
              {secondary.length > 0 && (
                <dl className="card-row-facts">
                  {secondary.map((c) => (
                    <div key={c.key}>
                      <dt>{c.title ?? c.header}</dt>
                      <dd>{c.cell(row)}</dd>
                    </div>
                  ))}
                </dl>
              )}
              {detail.length > 0 && (
                <Disclosure summary="Подробнее" className="inline">
                  <dl className="card-row-facts">
                    {detail.map((c) => (
                      <div key={c.key}>
                        <dt>{c.title ?? c.header}</dt>
                        <dd>{c.cell(row)}</dd>
                      </div>
                    ))}
                  </dl>
                </Disclosure>
              )}
            </li>
          ))}
        </ul>
      </div>
    );
  }

  return (
    <div className={`datatable ${className}`}>
      {menu}
      <div
        className={`table-scroll${stickyFirst ? " sticky-first" : ""}`}
        style={maxHeight ? { maxHeight } : undefined}
        tabIndex={0}
        role="region"
        aria-label={label}
      >
        <table style={minWidth ? { minWidth } : undefined}>
          <thead>
            <tr>
              {visible.map((c) => (
                <th
                  key={c.key}
                  scope="col"
                  className={`${c.align === "right" ? "align-right " : ""}${c.className ?? ""}`}
                >
                  {c.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={rowKey(row)} className={rowClassName?.(row)}>
                {visible.map((c) => (
                  <td
                    key={c.key}
                    className={`${c.align === "right" ? "align-right " : ""}${c.className ?? ""}`}
                  >
                    {c.cell(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
