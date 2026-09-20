import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { Check, ChevronDown, Search } from "lucide-react";
import { Popover } from "./Popover";
import "./controls.css";

export interface SelectOption<T extends string = string> {
  value: T;
  label: string;
  description?: string;
  group?: string;
  disabled?: boolean;
}

export interface SelectProps<T extends string = string> {
  value: T;
  onChange: (value: T) => void;
  options: SelectOption<T>[];
  /** Accessible name; also the sheet title on phones. */
  label: string;
  id?: string;
  placeholder?: string;
  disabled?: boolean;
  /** Show a search box; defaults to true when there are more than 8 options. */
  searchable?: boolean;
  className?: string;
  size?: "sm" | "md";
  /** Render something before the label inside the trigger (icon). */
  icon?: ReactNode;
  /** Trigger takes the full width of its container. */
  block?: boolean;
}

/** Custom listbox that looks and behaves the same in every browser. */
export function Select<T extends string = string>({
  value,
  onChange,
  options,
  label,
  id,
  placeholder = "Выберите…",
  disabled,
  searchable,
  className = "",
  size = "md",
  icon,
  block,
}: SelectProps<T>) {
  const autoId = useId();
  const listId = `${autoId}-list`;
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(-1);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const showSearch = searchable ?? options.length > 8;
  const selected = options.find((o) => o.value === value);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q
      ? options.filter((o) =>
          `${o.label} ${o.description ?? ""} ${o.value}`
            .toLowerCase()
            .includes(q),
        )
      : options;
  }, [options, query]);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    const index = visible.findIndex((o) => o.value === value);
    setActive(index >= 0 ? index : 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open || active < 0) return;
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${active}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const choose = (option: SelectOption<T>) => {
    if (option.disabled) return;
    onChange(option.value);
    setOpen(false);
  };

  const onKeyDown = (event: KeyboardEvent) => {
    if (!visible.length) return;
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        setActive((i) => (i + 1) % visible.length);
        break;
      case "ArrowUp":
        event.preventDefault();
        setActive((i) => (i - 1 + visible.length) % visible.length);
        break;
      case "Home":
        event.preventDefault();
        setActive(0);
        break;
      case "End":
        event.preventDefault();
        setActive(visible.length - 1);
        break;
      case "Enter":
        event.preventDefault();
        if (visible[active]) choose(visible[active]);
        break;
      case "Tab":
        setOpen(false);
        break;
      default:
        if (!showSearch && event.key.length === 1 && /\S/.test(event.key)) {
          const char = event.key.toLowerCase();
          const start = active + 1;
          const order = [...visible.slice(start), ...visible.slice(0, start)];
          const hit = order.find((o) => o.label.toLowerCase().startsWith(char));
          if (hit) setActive(visible.indexOf(hit));
        }
    }
  };

  let lastGroup: string | undefined;
  return (
    <>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        className={`select-trigger ${size}${block ? " block" : ""} ${className}`}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={label}
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(event) => {
          if ((event.key === "ArrowDown" || event.key === "ArrowUp") && !open) {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        {icon}
        <span className="select-value">
          {selected ? (
            <>
              {selected.label}
              {selected.description && <small>{selected.description}</small>}
            </>
          ) : (
            <span className="select-placeholder">{placeholder}</span>
          )}
        </span>
        <ChevronDown className="select-chevron" aria-hidden="true" />
      </button>
      <Popover
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={triggerRef}
        matchWidth
        role="none"
        className="select-popover"
        sheetTitle={label}
        initialFocus={() => (showSearch ? searchRef.current : listRef.current)}
      >
        <div className="select-panel" onKeyDown={onKeyDown}>
          {showSearch && (
            <label className="select-search">
              <Search aria-hidden="true" />
              <input
                ref={searchRef}
                type="search"
                value={query}
                placeholder="Поиск"
                aria-label={`Поиск: ${label}`}
                aria-controls={listId}
                aria-activedescendant={
                  visible[active] ? `${listId}-${active}` : undefined
                }
                autoComplete="off"
                onChange={(event) => {
                  setQuery(event.target.value);
                  setActive(0);
                }}
              />
            </label>
          )}
          <div
            ref={listRef}
            id={listId}
            role="listbox"
            aria-label={label}
            className="select-list"
            tabIndex={showSearch ? -1 : 0}
            aria-activedescendant={
              visible[active] ? `${listId}-${active}` : undefined
            }
          >
            {visible.length === 0 && (
              <div className="select-empty">Ничего не найдено</div>
            )}
            {visible.map((option, index) => {
              const groupHeader =
                option.group && option.group !== lastGroup
                  ? option.group
                  : null;
              lastGroup = option.group;
              return (
                <div key={option.value}>
                  {groupHeader && (
                    <div className="select-group" role="presentation">
                      {groupHeader}
                    </div>
                  )}
                  <div
                    id={`${listId}-${index}`}
                    role="option"
                    aria-selected={option.value === value}
                    aria-disabled={option.disabled || undefined}
                    data-index={index}
                    className={`select-option${index === active ? " active" : ""}${
                      option.disabled ? " disabled" : ""
                    }`}
                    onPointerMove={() => setActive(index)}
                    onClick={() => choose(option)}
                  >
                    <span className="select-option-text">
                      {option.label}
                      {option.description && (
                        <small>{option.description}</small>
                      )}
                    </span>
                    {option.value === value && (
                      <Check className="select-check" aria-hidden="true" />
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </Popover>
    </>
  );
}
