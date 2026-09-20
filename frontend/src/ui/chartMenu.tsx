import { Clock, Copy } from "lucide-react";
import type { ContextMenuItem } from "./ContextMenu";
import { formatChartTimestamp } from "../visualization";

/** Standard chart context menu: open the moment under the pointer, copy time. */
export function timeMenu(
  onSelectTime?: (time: string) => void,
): (time: string | null) => ContextMenuItem[] | null {
  return (time) => {
    if (!time) return null;
    const label = formatChartTimestamp(Date.parse(time + "Z"));
    return [
      ...(onSelectTime
        ? [
            {
              label: `Открыть момент ${label}`,
              icon: <Clock />,
              onSelect: () => onSelectTime(time),
            },
          ]
        : []),
      {
        label: "Скопировать время",
        icon: <Copy />,
        onSelect: () => {
          void navigator.clipboard?.writeText(label);
        },
      },
    ];
  };
}
