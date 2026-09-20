import { useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import {
  useEscape,
  useFocusTrap,
  useInertOutside,
  useScrollLock,
  useViewport,
} from "./hooks";
import { Sheet } from "./Sheet";
import "./layers.css";

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  /** modal: centred; drawer: right-hand panel. Both become a sheet on phones. */
  variant?: "modal" | "drawer";
  width?: number;
  className?: string;
  /** Extra controls rendered in the header next to the close button. */
  headerActions?: ReactNode;
}

/** The single modal layer implementation: focus trap, inert page, Escape. */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  variant = "modal",
  width,
  className = "",
  headerActions,
}: DialogProps) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  const viewport = useViewport();
  useEscape(onClose, open);
  useFocusTrap(ref, open && viewport !== "phone", { initialFocus: "first" });
  useScrollLock(open && viewport !== "phone");
  useInertOutside(ref, open && viewport !== "phone");
  if (!open) return null;
  if (viewport === "phone")
    return (
      <Sheet
        open
        onClose={onClose}
        title={title}
        description={typeof description === "string" ? description : undefined}
        size="tall"
        className={className}
      >
        {description && typeof description !== "string" && (
          <div className="layer-description">{description}</div>
        )}
        {children}
        {footer && <footer className="layer-footer">{footer}</footer>}
      </Sheet>
    );
  return createPortal(
    <div
      className={`layer-backdrop ${variant}-backdrop`}
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={ref}
        className={`${variant} ${className}`}
        style={width ? { width: `min(${width}px, 100vw - 32px)` } : undefined}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        aria-describedby={description ? `${id}-desc` : undefined}
        tabIndex={-1}
      >
        <header className="layer-header">
          <div>
            <h2 id={`${id}-title`}>{title}</h2>
            {description && <p id={`${id}-desc`}>{description}</p>}
          </div>
          <div className="layer-header-actions">
            {headerActions}
            <button
              type="button"
              className="icon-button"
              aria-label="Закрыть"
              onClick={onClose}
            >
              <X />
            </button>
          </div>
        </header>
        <div className="layer-body">{children}</div>
        {footer && <footer className="layer-footer">{footer}</footer>}
      </div>
    </div>,
    document.body,
  );
}
