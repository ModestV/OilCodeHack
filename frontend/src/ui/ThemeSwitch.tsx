import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme, type ThemePreference } from "./useTheme";

const options: { value: ThemePreference; label: string; Icon: typeof Sun }[] = [
  { value: "system", label: "Как в системе", Icon: Monitor },
  { value: "light", label: "Светлая тема", Icon: Sun },
  { value: "dark", label: "Тёмная тема", Icon: Moon },
];

export function ThemeSwitch({ compact = false }: { compact?: boolean }) {
  const { preference, setPreference } = useTheme();
  return (
    <div
      className={`theme-switch${compact ? " compact" : ""}`}
      role="radiogroup"
      aria-label="Тема оформления"
    >
      {options.map(({ value, label, Icon }) => (
        <button
          key={value}
          type="button"
          role="radio"
          aria-checked={preference === value}
          aria-label={label}
          title={label}
          className={preference === value ? "active" : ""}
          onClick={() => setPreference(value)}
        >
          <Icon />
        </button>
      ))}
    </div>
  );
}
