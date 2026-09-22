import { Check, ChevronDown } from "lucide-react";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import "./Controls.css";

export type SelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
};

export function UiSelect({
  value,
  options,
  onChange,
  ariaLabel,
  className = "",
  disabled = false,
  menuMinWidth = 190,
}: {
  value: string;
  options: readonly SelectOption[];
  onChange: (value: string) => void;
  ariaLabel: string;
  className?: string;
  disabled?: boolean;
  menuMinWidth?: number;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [placement, setPlacement] = useState({ left: 0, top: 0, width: 190 });
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const listId = useId();
  const selectedIndex = useMemo(
    () => Math.max(0, options.findIndex((option) => option.value === value)),
    [options, value],
  );
  const selected = options[selectedIndex];

  useEffect(() => {
    if (!open) return;
    setActive(selectedIndex);
    const close = (event: PointerEvent) => {
      const target = event.target as Node;
      if (target instanceof Element && target.closest(".ui-select, .ui-select__menu")) return;
      if (!root.current?.contains(target) && !menu.current?.contains(target))
        setOpen(false);
    };
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    const closeOnViewportChange = (event: Event) => {
      if (event.type === "scroll" && menu.current?.contains(event.target as Node)) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", escape);
    window.addEventListener("resize", closeOnViewportChange);
    window.addEventListener("scroll", closeOnViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", escape);
      window.removeEventListener("resize", closeOnViewportChange);
      window.removeEventListener("scroll", closeOnViewportChange, true);
    };
  }, [open, selectedIndex]);

  useLayoutEffect(() => {
    if (!open || !trigger.current || !menu.current) return;
    const anchor = trigger.current.getBoundingClientRect();
    const box = menu.current.getBoundingClientRect();
    const width = Math.min(Math.max(anchor.width, menuMinWidth), window.innerWidth - 16);
    const left = Math.max(8, Math.min(anchor.left, window.innerWidth - width - 8));
    const spaceBelow = window.innerHeight - anchor.bottom - 8;
    const top = spaceBelow >= box.height || spaceBelow >= anchor.top
      ? Math.min(anchor.bottom + 4, window.innerHeight - box.height - 8)
      : Math.max(8, anchor.top - box.height - 4);
    setPlacement({ left, top: Math.max(8, top), width });
    const selectedOption = menu.current.querySelector<HTMLElement>(
      `[data-option-index="${selectedIndex}"]`,
    );
    if (selectedOption) {
      menu.current.scrollTop = Math.max(
        0,
        selectedOption.offsetTop - menu.current.clientHeight / 2,
      );
    }
  }, [menuMinWidth, open, selectedIndex]);

  function toggle() {
    if (open) {
      setOpen(false);
      return;
    }
    const anchor = trigger.current?.getBoundingClientRect();
    if (anchor) {
      setPlacement({
        left: Math.max(8, anchor.left),
        top: anchor.bottom + 4,
        width: Math.min(Math.max(anchor.width, menuMinWidth), window.innerWidth - 16),
      });
    }
    setOpen(true);
  }

  function choose(index: number) {
    const option = options[index];
    if (!option || option.disabled) return;
    onChange(option.value);
    setOpen(false);
    trigger.current?.focus();
  }

  function move(direction: 1 | -1) {
    if (!options.length) return;
    let next = active;
    for (let index = 0; index < options.length; index += 1) {
      next = (next + direction + options.length) % options.length;
      if (!options[next].disabled) break;
    }
    setActive(next);
  }

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        setActive(selectedIndex);
      } else {
        move(event.key === "ArrowDown" ? 1 : -1);
      }
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      setOpen(true);
      setActive(event.key === "Home" ? 0 : options.length - 1);
      return;
    }
    if ((event.key === "Enter" || event.key === " ") && open) {
      event.preventDefault();
      choose(active);
    }
  }

  return (
    <div className={`ui-select ${className}`.trim()} ref={root}>
      <button
        ref={trigger}
        type="button"
        className="ui-select__trigger"
        role="combobox"
        aria-label={ariaLabel}
        aria-expanded={open}
        aria-controls={listId}
        aria-haspopup="listbox"
        disabled={disabled}
        onClick={toggle}
        onKeyDown={onKeyDown}
      >
        <span>{selected?.label ?? "Выберите значение"}</span>
        <ChevronDown aria-hidden="true" />
      </button>
      {open && createPortal(
        <div
          ref={menu}
          className="ui-select__menu"
          id={listId}
          role="listbox"
          style={placement}
          onPointerDown={(event) => event.stopPropagation()}
          >
          {options.map((option, index) => (
            <button
              type="button"
              role="option"
              aria-selected={option.value === value}
              className={index === active ? "is-active" : ""}
              disabled={option.disabled}
              key={option.value}
              data-option-index={index}
              onPointerMove={() => setActive(index)}
              onClick={() => choose(index)}
            >
              <span>{option.label}</span>
              {option.value === value ? <Check aria-hidden="true" /> : null}
            </button>
          ))}
        </div>,
        document.body,
      )}
    </div>
  );
}
