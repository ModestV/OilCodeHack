import {
  cloneElement,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactElement,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import "./controls.css";

interface TooltipProps {
  text: ReactNode;
  children: ReactElement;
  delay?: number;
}

/**
 * Replacement for title="": same look everywhere, opens on hover/focus after a
 * short delay, on tap for touch, closes on Escape. The child gets
 * aria-describedby so screen readers read the text.
 */
export function Tooltip({ text, children, delay = 300 }: TooltipProps) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [style, setStyle] = useState<CSSProperties>();
  const anchor = useRef<HTMLElement | null>(null);
  const bubble = useRef<HTMLDivElement>(null);
  const timer = useRef<number>();

  const show = () => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setOpen(true), delay);
  };
  const hide = () => {
    window.clearTimeout(timer.current);
    setOpen(false);
  };

  useLayoutEffect(() => {
    if (!open || !anchor.current || !bubble.current) return;
    const a = anchor.current.getBoundingClientRect();
    const b = bubble.current.getBoundingClientRect();
    const top = a.top - b.height - 8 >= 0 ? a.top - b.height - 8 : a.bottom + 8;
    const left = Math.min(
      Math.max(8, a.left + a.width / 2 - b.width / 2),
      window.innerWidth - b.width - 8,
    );
    setStyle({ top, left });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && hide();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  const child = cloneElement(children, {
    ref: (node: HTMLElement | null) => {
      anchor.current = node;
      const { ref } = children as unknown as { ref?: unknown };
      if (typeof ref === "function") ref(node);
      else if (ref && typeof ref === "object")
        (ref as { current: HTMLElement | null }).current = node;
    },
    "aria-describedby": open ? id : undefined,
    onMouseEnter: (e: React.MouseEvent) => {
      children.props.onMouseEnter?.(e);
      show();
    },
    onMouseLeave: (e: React.MouseEvent) => {
      children.props.onMouseLeave?.(e);
      hide();
    },
    onFocus: (e: React.FocusEvent) => {
      children.props.onFocus?.(e);
      show();
    },
    onBlur: (e: React.FocusEvent) => {
      children.props.onBlur?.(e);
      hide();
    },
    onPointerDown: (e: React.PointerEvent) => {
      children.props.onPointerDown?.(e);
      if (e.pointerType === "touch") {
        window.clearTimeout(timer.current);
        setOpen((v) => !v);
      }
    },
  } as Record<string, unknown>);

  return (
    <>
      {child}
      {open &&
        createPortal(
          <div
            ref={bubble}
            id={id}
            role="tooltip"
            className="tooltip"
            style={style}
          >
            {text}
          </div>,
          document.body,
        )}
    </>
  );
}
