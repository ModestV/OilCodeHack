import {
  createContext,
  useCallback,
  useContext,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { Check, ChevronDown } from "lucide-react";
import { Popover, type Placement } from "./Popover";
import "./controls.css";

const MenuContext = createContext<{ close: () => void } | null>(null);

export interface MenuProps {
  /** Visible trigger content. */
  label: ReactNode;
  /** Accessible name when the label is not descriptive (icon-only). */
  ariaLabel?: string;
  icon?: ReactNode;
  chevron?: boolean;
  children: ReactNode;
  placement?: Placement;
  className?: string;
  triggerClassName?: string;
  sheetTitle?: string;
  disabled?: boolean;
}

function focusItems(root: HTMLElement | null) {
  return Array.from(
    root?.querySelectorAll<HTMLElement>(
      '[role="menuitem"]:not([disabled]),[role="menuitemradio"]:not([disabled])',
    ) ?? [],
  );
}

/** Dropdown menu: button trigger + roving-focus list. Replaces <details> menus. */
export function Menu({
  label,
  ariaLabel,
  icon,
  chevron = true,
  children,
  placement = "bottom-end",
  className = "",
  triggerClassName = "",
  sheetTitle,
  disabled,
}: MenuProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const id = useId();
  const close = useCallback(() => setOpen(false), []);
  const onKeyDown = (event: KeyboardEvent) => {
    const items = focusItems(listRef.current);
    if (!items.length) return;
    const index = items.indexOf(document.activeElement as HTMLElement);
    let next: number | null = null;
    if (event.key === "ArrowDown") next = (index + 1) % items.length;
    else if (event.key === "ArrowUp")
      next = (index - 1 + items.length) % items.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = items.length - 1;
    else if (event.key === "Tab") {
      close();
      return;
    }
    if (next != null) {
      event.preventDefault();
      items[next].focus();
    }
  };
  return (
    <MenuContext.Provider value={{ close }}>
      <button
        ref={triggerRef}
        type="button"
        className={`menu-trigger ${triggerClassName}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" && !open) {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        {icon}
        {label}
        {chevron && <ChevronDown className="menu-chevron" aria-hidden="true" />}
      </button>
      <Popover
        open={open}
        onClose={close}
        anchorRef={triggerRef}
        placement={placement}
        role="none"
        className={`menu-popover ${className}`}
        sheetTitle={sheetTitle}
        initialFocus={() => focusItems(listRef.current)[0] ?? null}
      >
        <div
          ref={listRef}
          id={id}
          role="menu"
          className="menu-list"
          onKeyDown={onKeyDown}
        >
          {children}
        </div>
      </Popover>
    </MenuContext.Provider>
  );
}

export interface MenuItemProps {
  children: ReactNode;
  onSelect?: () => void;
  icon?: ReactNode;
  description?: ReactNode;
  /** Renders as a radio item with a check mark. */
  selected?: boolean;
  disabled?: boolean;
  /** Keep the menu open after selecting. */
  keepOpen?: boolean;
  danger?: boolean;
}

export function MenuItem({
  children,
  onSelect,
  icon,
  description,
  selected,
  disabled,
  keepOpen,
  danger,
}: MenuItemProps) {
  const menu = useContext(MenuContext);
  return (
    <button
      type="button"
      role={selected === undefined ? "menuitem" : "menuitemradio"}
      aria-checked={selected === undefined ? undefined : selected}
      className={`menu-item${danger ? " danger" : ""}`}
      disabled={disabled}
      tabIndex={-1}
      onClick={() => {
        onSelect?.();
        if (!keepOpen) menu?.close();
      }}
    >
      {icon && <span className="menu-item-icon">{icon}</span>}
      <span className="menu-item-text">
        {children}
        {description && <small>{description}</small>}
      </span>
      {selected && <Check className="menu-item-check" aria-hidden="true" />}
    </button>
  );
}

export function MenuSeparator() {
  return <div role="separator" className="menu-separator" />;
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <div className="menu-label">{children}</div>;
}
