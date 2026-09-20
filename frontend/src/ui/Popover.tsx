import {
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { useEscape, useFocusTrap, useOutsideClick, useViewport } from "./hooks";
import { Sheet } from "./Sheet";
import "./layers.css";

export type Placement = "bottom-start" | "bottom-end" | "top-start" | "top-end";

export interface PopoverProps {
  open: boolean;
  onClose: () => void;
  anchorRef: RefObject<HTMLElement | null>;
  children: ReactNode;
  placement?: Placement;
  /** Match the anchor's width (selects). */
  matchWidth?: boolean;
  /** Below this width on touch devices the popover becomes a bottom sheet. */
  sheetOnPhone?: boolean;
  sheetTitle?: string;
  role?: "dialog" | "listbox" | "menu" | "none";
  label?: string;
  labelledBy?: string;
  /** Where focus goes on open. Default: first focusable element. */
  initialFocus?: "first" | "container" | (() => HTMLElement | null);
  trapFocus?: boolean;
  className?: string;
  offset?: number;
}

const GAP = 8;

function position(
  anchor: DOMRect,
  panel: { width: number; height: number },
  placement: Placement,
  offset: number,
): CSSProperties {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const wantsTop = placement.startsWith("top");
  const spaceBelow = vh - anchor.bottom - offset;
  const spaceAbove = anchor.top - offset;
  const top =
    (wantsTop && spaceAbove >= panel.height) ||
    (!wantsTop && spaceBelow < panel.height && spaceAbove > spaceBelow)
      ? anchor.top - offset - panel.height
      : anchor.bottom + offset;
  const alignEnd = placement.endsWith("end");
  let left = alignEnd ? anchor.right - panel.width : anchor.left;
  left = Math.min(Math.max(GAP, left), vw - panel.width - GAP);
  const maxHeight = Math.max(
    120,
    top < anchor.top ? anchor.top - offset - GAP : vh - top - GAP,
  );
  return {
    top: Math.max(GAP, top),
    left,
    maxHeight,
  };
}

/**
 * Anchored floating panel rendered in a portal. Flips to the side with more
 * room, clamps to the viewport, closes on Escape / outside pointer, restores
 * focus, and turns into a bottom sheet on phones.
 */
export function Popover({
  open,
  onClose,
  anchorRef,
  children,
  placement = "bottom-start",
  matchWidth = false,
  sheetOnPhone = true,
  sheetTitle,
  role = "dialog",
  label,
  labelledBy,
  initialFocus = "first",
  trapFocus = true,
  className = "",
  offset = 6,
}: PopoverProps) {
  const viewport = useViewport();
  const panelRef = useRef<HTMLDivElement>(null);
  const [style, setStyle] = useState<CSSProperties>();
  const phone = viewport === "phone" && sheetOnPhone;

  useLayoutEffect(() => {
    if (!open || phone) return;
    const update = () => {
      const anchor = anchorRef.current?.getBoundingClientRect();
      const panel = panelRef.current;
      if (!anchor || !panel) return;
      const width = matchWidth
        ? Math.max(anchor.width, 160)
        : panel.offsetWidth;
      const next = position(
        anchor,
        { width, height: panel.offsetHeight },
        placement,
        offset,
      );
      setStyle(matchWidth ? { ...next, width } : next);
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [open, phone, anchorRef, matchWidth, placement, offset, children]);

  useEscape(onClose, open && !phone);
  useOutsideClick([panelRef, anchorRef], onClose, open && !phone);
  useFocusTrap(panelRef, open && !phone && trapFocus, { initialFocus });

  if (!open) return null;
  if (phone)
    return (
      <Sheet
        open
        onClose={onClose}
        title={sheetTitle ?? label}
        className={className}
      >
        {children}
      </Sheet>
    );
  return createPortal(
    <div
      ref={panelRef}
      className={`popover ${className}`}
      style={style}
      role={role === "none" ? undefined : role}
      aria-label={label}
      aria-labelledby={labelledBy}
      tabIndex={-1}
      data-placement={placement}
    >
      {children}
    </div>,
    document.body,
  );
}
