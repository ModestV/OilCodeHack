import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useSyncExternalStore,
  type RefObject,
} from "react";

const queries = new Map<string, MediaQueryList>();
const mql = (query: string) => {
  let list = queries.get(query);
  if (!list) {
    list = matchMedia(query);
    queries.set(query, list);
  }
  return list;
};

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      const list = mql(query);
      list.addEventListener("change", notify);
      return () => list.removeEventListener("change", notify);
    },
    () => mql(query).matches,
    () => false,
  );
}

export type Viewport = "phone" | "tablet" | "desktop";
export const PHONE_MAX = 599;
export const TABLET_MAX = 899;

export function useViewport(): Viewport {
  const phone = useMediaQuery(`(max-width: ${PHONE_MAX}px)`);
  const tablet = useMediaQuery(`(max-width: ${TABLET_MAX}px)`);
  return phone ? "phone" : tablet ? "tablet" : "desktop";
}

/** Coarse pointer (touch) — hover is unreliable, targets must be larger. */
export function useCoarsePointer(): boolean {
  return useMediaQuery("(pointer: coarse)");
}

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]):not([type="hidden"]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"]),summary';

export function focusables(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.offsetParent !== null || el === document.activeElement,
  );
}

/**
 * Keeps Tab focus inside `ref` while `active`, moves focus in on open and
 * restores it to the previously focused element on close.
 */
export function useFocusTrap(
  ref: RefObject<HTMLElement | null>,
  active: boolean,
  options: {
    initialFocus?: "first" | "container" | (() => HTMLElement | null);
  } = {},
) {
  const restore = useRef<HTMLElement | null>(null);
  const { initialFocus = "first" } = options;
  useLayoutEffect(() => {
    if (!active) return;
    restore.current = document.activeElement as HTMLElement | null;
    const node = ref.current;
    if (!node) return;
    const target =
      typeof initialFocus === "function"
        ? initialFocus()
        : initialFocus === "first"
          ? focusables(node)[0] || node
          : node;
    // Defer so opening animations / portals have laid out.
    const id = requestAnimationFrame(() =>
      target?.focus({ preventScroll: true }),
    );
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const items = focusables(node);
      if (!items.length) {
        event.preventDefault();
        node.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const current = document.activeElement;
      if (event.shiftKey && (current === first || current === node)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && current === last) {
        event.preventDefault();
        first.focus();
      }
    };
    node.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(id);
      node.removeEventListener("keydown", onKey);
      const back = restore.current;
      if (back && document.contains(back)) back.focus({ preventScroll: true });
    };
  }, [active, ref, initialFocus]);
}

/** Calls `handler` on pointerdown outside every ref in `refs`. */
export function useOutsideClick(
  refs: RefObject<Element | null>[],
  handler: () => void,
  active = true,
) {
  const latest = useRef(handler);
  latest.current = handler;
  useEffect(() => {
    if (!active) return;
    const onDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (refs.some((r) => r.current?.contains(target))) return;
      latest.current();
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [active, refs]);
}

/** Escape closes; stops propagation so nested layers close one at a time. */
export function useEscape(handler: () => void, active = true) {
  const latest = useRef(handler);
  latest.current = handler;
  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      latest.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [active]);
}

let locks = 0;
/** Prevents body scroll while a modal layer is open (counts nested layers). */
export function useScrollLock(active: boolean) {
  useLayoutEffect(() => {
    if (!active) return;
    locks += 1;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      locks -= 1;
      if (locks === 0) document.body.style.overflow = previous;
    };
  }, [active]);
}

/** Makes everything except `keep` inert (screen readers and Tab skip it). */
export function useInertOutside(
  keep: RefObject<HTMLElement | null>,
  active: boolean,
) {
  useLayoutEffect(() => {
    if (!active) return;
    const root = document.getElementById("root");
    if (!root || keep.current?.contains(root) || root.contains(keep.current!))
      return;
    root.setAttribute("inert", "");
    return () => root.removeAttribute("inert");
  }, [active, keep]);
}

/** Roving keyboard navigation over a list of elements (menus, listboxes). */
export function useRovingIndex(
  count: number,
  onSelect?: (index: number) => void,
) {
  const indexRef = useRef(-1);
  const move = useCallback(
    (event: React.KeyboardEvent, current: number): number | null => {
      if (!count) return null;
      switch (event.key) {
        case "ArrowDown":
          event.preventDefault();
          return (current + 1) % count;
        case "ArrowUp":
          event.preventDefault();
          return (current - 1 + count) % count;
        case "Home":
          event.preventDefault();
          return 0;
        case "End":
          event.preventDefault();
          return count - 1;
        case "Enter":
        case " ":
          if (current >= 0) {
            event.preventDefault();
            onSelect?.(current);
          }
          return null;
        default:
          return null;
      }
    },
    [count, onSelect],
  );
  return { move, indexRef };
}
