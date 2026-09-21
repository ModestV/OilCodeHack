import { useEffect, useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";
import "./Controls.css";

export type ContextAction = {
  label: string;
  onSelect: () => void | Promise<void>;
  disabled?: boolean;
  destructive?: boolean;
};

export function ContextMenu({
  position,
  actions,
  onClose,
  label = "Контекстное меню",
}: {
  position: { x: number; y: number } | null;
  actions: readonly ContextAction[];
  onClose: () => void;
  label?: string;
}) {
  const menu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!position) return;
    const close = (event: PointerEvent) => {
      if (!menu.current?.contains(event.target as Node)) onClose();
    };
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", keyboard);
    window.addEventListener("blur", onClose);
    return () => {
      window.removeEventListener("pointerdown", close);
      window.removeEventListener("keydown", keyboard);
      window.removeEventListener("blur", onClose);
    };
  }, [onClose, position]);

  useLayoutEffect(() => {
    if (!position || !menu.current) return;
    const box = menu.current.getBoundingClientRect();
    menu.current.style.left = `${Math.max(8, Math.min(position.x, window.innerWidth - box.width - 8))}px`;
    menu.current.style.top = `${Math.max(8, Math.min(position.y, window.innerHeight - box.height - 8))}px`;
    menu.current.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
  }, [position]);

  if (!position) return null;
  return createPortal(
    <div
      ref={menu}
      className="context-menu"
      role="menu"
      aria-label={label}
      style={{ left: position.x, top: position.y }}
    >
      {actions.map((action) => (
        <button
          type="button"
          role="menuitem"
          className={action.destructive ? "is-destructive" : ""}
          disabled={action.disabled}
          key={action.label}
          onClick={async () => {
            await action.onSelect();
            onClose();
          }}
        >
          {action.label}
        </button>
      ))}
    </div>,
    document.body,
  );
}
