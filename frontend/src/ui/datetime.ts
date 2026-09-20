/* Pure helpers for the masked date-time field and the calendar. All values are
   naive local ISO strings ("YYYY-MM-DDTHH:MM:SS"), as used by the API. */

export const MASK = "ДД.ММ.ГГГГ ЧЧ:ММ";

/** Keeps digits only and lays them out as DD.MM.YYYY HH:MM while typing. */
export function applyMask(raw: string): string {
  const digits = raw.replace(/\D/g, "").slice(0, 12);
  const parts = [
    digits.slice(0, 2),
    digits.slice(2, 4),
    digits.slice(4, 8),
    digits.slice(8, 10),
    digits.slice(10, 12),
  ];
  let out = parts[0];
  if (digits.length > 2) out += "." + parts[1];
  if (digits.length > 4) out += "." + parts[2];
  if (digits.length > 8) out += " " + parts[3];
  if (digits.length > 10) out += ":" + parts[4];
  return out;
}

export function formatMasked(iso: string | null | undefined): string {
  if (!iso) return "";
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  return m ? `${m[3]}.${m[2]}.${m[1]} ${m[4]}:${m[5]}` : "";
}

const pad = (n: number) => String(n).padStart(2, "0");

export function toIso(y: number, mo: number, d: number, h = 0, mi = 0, s = 0) {
  return `${y}-${pad(mo)}-${pad(d)}T${pad(h)}:${pad(mi)}:${pad(s)}`;
}

export function daysInMonth(year: number, month: number) {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

/** Parses a fully typed masked value; returns null when incomplete or invalid. */
export function parseMasked(text: string): string | null {
  const m = text.match(/^(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})$/);
  if (!m) return null;
  const [d, mo, y, h, mi] = m.slice(1).map(Number);
  if (mo < 1 || mo > 12 || d < 1 || d > daysInMonth(y, mo)) return null;
  if (h > 23 || mi > 59) return null;
  return toIso(y, mo, d, h, mi);
}

export function parts(iso: string) {
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!m) return null;
  const [y, mo, d, h, mi] = m.slice(1).map(Number);
  return { y, mo, d, h, mi };
}

export function clampIso(iso: string, min?: string, max?: string) {
  if (min && iso < min) return min;
  if (max && iso > max) return max;
  return iso;
}

export const MONTHS = [
  "Январь",
  "Февраль",
  "Март",
  "Апрель",
  "Май",
  "Июнь",
  "Июль",
  "Август",
  "Сентябрь",
  "Октябрь",
  "Ноябрь",
  "Декабрь",
];
export const WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

/** 6 rows × 7 days, Monday first; days outside the month are null. */
export function monthGrid(year: number, month: number): (number | null)[][] {
  const first = new Date(Date.UTC(year, month - 1, 1)).getUTCDay(); // 0=Sun
  const lead = (first + 6) % 7;
  const total = daysInMonth(year, month);
  const cells: (number | null)[] = [
    ...Array<null>(lead).fill(null),
    ...Array.from({ length: total }, (_, i) => i + 1),
  ];
  while (cells.length % 7) cells.push(null);
  const rows: (number | null)[][] = [];
  for (let i = 0; i < cells.length; i += 7) rows.push(cells.slice(i, i + 7));
  return rows;
}

/** Adds minutes to a naive ISO string without timezone conversion. */
export function addMinutes(iso: string, minutes: number): string {
  const t = Date.parse(iso.replace(/Z$/, "") + "Z") + minutes * 60000;
  return new Date(t).toISOString().slice(0, 19);
}
