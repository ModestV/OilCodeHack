import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CircleHelp } from "lucide-react";

export function HelpTooltip({
  label,
  children,
}: {
  label: string;
  children: string;
}) {
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const tooltip = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const button = trigger.current?.getBoundingClientRect();
      const popup = tooltip.current;
      if (!button || !popup) return;
      const gap = 8;
      const left = Math.max(
        12,
        Math.min(
          button.left + button.width / 2 - popup.offsetWidth / 2,
          window.innerWidth - popup.offsetWidth - 12,
        ),
      );
      const fitsBelow =
        button.bottom + gap + popup.offsetHeight < window.innerHeight - 12;
      popup.style.left = `${left}px`;
      popup.style.top = `${fitsBelow ? button.bottom + gap : button.top - popup.offsetHeight - gap}px`;
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!trigger.current?.contains(event.target as Node)) {
        setPinned(false);
        setOpen(false);
      }
    };
    const closeEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        setPinned(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeEscape);
    };
  }, [open]);

  return (
    <span className="help-tooltip">
      <button
        ref={trigger}
        type="button"
        className="help-tooltip__trigger"
        aria-label={`Справка: ${label}`}
        aria-describedby={open ? id : undefined}
        aria-expanded={open}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => {
          if (!pinned) setOpen(false);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={(event) => {
          event.stopPropagation();
          setPinned((value) => {
            const next = !value;
            setOpen(next);
            return next;
          });
        }}
      >
        <CircleHelp aria-hidden="true" />
      </button>
      {open &&
        createPortal(
          <div
            id={id}
            ref={tooltip}
            className="help-tooltip__content"
            role="tooltip"
          >
            {children}
          </div>,
          document.body,
        )}
    </span>
  );
}
