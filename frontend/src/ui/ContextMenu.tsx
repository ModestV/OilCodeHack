import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { useEscape, useOutsideClick, useViewport } from "./hooks";
import { Sheet } from "./Sheet";
import "./controls.css";
import "./layers.css";

export interface ContextMenuItem {
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  disabled?: boolean;
  danger?: boolean;
}

interface Anchor {
  x: number;
  y: number;
}

/**
 * Right-click (or long-press on touch) context menu. Our own menu rather than
 * the browser's, so the actions and look are the same everywhere.
 */
export function ContextMenu({
  items,
  children,
  className = "",
  label = "Действия",
  /** Called with the pointer event so callers can capture what was hit. */
  onOpen,
}: {
  items:
    | ContextMenuItem[]
    | ((event: MouseEvent | PointerEvent) => ContextMenuItem[] | null);
  children: ReactNode;
  className?: string;
  label?: string;
  onOpen?: (event: MouseEvent | PointerEvent) => void;
}) {
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  const [resolved, setResolved] = useState<ContextMenuItem[]>([]);
  const menuRef = useRef<HTMLDivElement>(null);
  const press = useRef<number>();
  const viewport = useViewport();
  const close = useCallback(() => setAnchor(null), []);

  const openAt = (event: MouseEvent | PointerEvent, x: number, y: number) => {
    const list = typeof items === "function" ? items(event) : items;
    if (!list || !list.length) return;
    onOpen?.(event);
    setResolved(list);
    setAnchor({ x, y });
  };

  useEscape(close, anchor != null);
  useOutsideClick([menuRef], close, anchor != null && viewport !== "phone");
  useEffect(() => {
    if (!anchor || viewport === "phone") return;
    const menu = menuRef.current;
    if (!menu) return;
    const rect = menu.getBoundingClientRect();
    const x = Math.min(anchor.x, window.innerWidth - rect.width - 8);
    const y = Math.min(anchor.y, window.innerHeight - rect.height - 8);
    menu.style.left = `${Math.max(8, x)}px`;
    menu.style.top = `${Math.max(8, y)}px`;
    menu.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
  }, [anchor, viewport]);

  const onKeyDown = (event: KeyboardEvent) => {
    const els = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>(
        '[role="menuitem"]:not([disabled])',
      ) ?? [],
    );
    const i = els.indexOf(document.activeElement as HTMLElement);
    if (event.key === "ArrowDown") els[(i + 1) % els.length]?.focus();
    else if (event.key === "ArrowUp")
      els[(i - 1 + els.length) % els.length]?.focus();
    else return;
    event.preventDefault();
  };

  const startPress = (event: ReactPointerEvent) => {
    if (event.pointerType !== "touch") return;
    const { clientX, clientY, nativeEvent } = event;
    press.current = window.setTimeout(
      () => openAt(nativeEvent, clientX, clientY),
      500,
    );
  };
  const cancelPress = () => window.clearTimeout(press.current);

  const list = (
    <div
      ref={menuRef}
      role="menu"
      aria-label={label}
      className={
        viewport === "phone" ? "menu-list" : "popover menu-popover context-menu"
      }
      onKeyDown={onKeyDown}
    >
      {resolved.map((item) => (
        <button
          key={item.label}
          type="button"
          role="menuitem"
          tabIndex={-1}
          className={`menu-item${item.danger ? " danger" : ""}`}
          disabled={item.disabled}
          onClick={() => {
            close();
            item.onSelect();
          }}
        >
          {item.icon && <span className="menu-item-icon">{item.icon}</span>}
          <span className="menu-item-text">{item.label}</span>
        </button>
      ))}
    </div>
  );

  return (
    <div
      className={`context-menu-target ${className}`}
      onContextMenu={(event) => {
        event.preventDefault();
        openAt(event.nativeEvent, event.clientX, event.clientY);
      }}
      onPointerDown={startPress}
      onPointerUp={cancelPress}
      onPointerCancel={cancelPress}
      onPointerMove={cancelPress}
    >
      {children}
      {anchor &&
        (viewport === "phone" ? (
          <Sheet open onClose={close} title={label}>
            {list}
          </Sheet>
        ) : (
          createPortal(list, document.body)
        ))}
    </div>
  );
}
