import { useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import {
  useEscape,
  useFocusTrap,
  useInertOutside,
  useScrollLock,
} from "./hooks";
import "./layers.css";

export interface SheetProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  description?: string;
  children: ReactNode;
  className?: string;
  /** Tall sheets (filters, settings) take most of the screen. */
  size?: "auto" | "tall";
  labelledBy?: string;
}

/** Bottom sheet for phones: full width, safe-area aware, focus trapped. */
export function Sheet({
  open,
  onClose,
  title,
  description,
  children,
  className = "",
  size = "auto",
  labelledBy,
}: SheetProps) {
  const ref = useRef<HTMLDivElement>(null);
  useEscape(onClose, open);
  useFocusTrap(ref, open, { initialFocus: "container" });
  useScrollLock(open);
  useInertOutside(ref, open);
  if (!open) return null;
  return createPortal(
    <div className="layer-backdrop sheet-backdrop" onPointerDown={onClose}>
      <div
        ref={ref}
        className={`sheet ${size} ${className}`}
        role="dialog"
        aria-modal="true"
        aria-label={labelledBy ? undefined : title}
        aria-labelledby={labelledBy}
        tabIndex={-1}
        onPointerDown={(e) => e.stopPropagation()}
      >
        <div className="sheet-grip" aria-hidden="true" />
        {(title || description) && (
          <header className="layer-header">
            <div>
              {title && <h2>{title}</h2>}
              {description && <p>{description}</p>}
            </div>
            <button
              type="button"
              className="icon-button"
              aria-label="Закрыть"
              onClick={onClose}
            >
              <X />
            </button>
          </header>
        )}
        <div className="layer-body">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
