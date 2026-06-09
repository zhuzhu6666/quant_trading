import { Skeleton } from "./Skeleton";
import { classNames } from "@/lib/format";

export interface Column<T> {
  key: string;
  header: string;
  render: (item: T, index: number) => React.ReactNode;
  align?: "left" | "right" | "center";
  width?: string;
}

interface TableProps<T> {
  columns: Column<T>[];
  data: T[];
  keyExtractor: (item: T, index: number) => string;
  loading?: boolean;
  emptyMessage?: string;
  onRowClick?: (item: T) => void;
  selectedKey?: string;
  className?: string;
}

export function Table<T>({
  columns,
  data,
  keyExtractor,
  loading,
  emptyMessage = "暂无数据",
  onRowClick,
  selectedKey,
  className,
}: TableProps<T>) {
  if (loading) {
    return (
      <div className="bg-[#1c2128] border border-border rounded-lg overflow-hidden">
        <Skeleton variant="table-row" count={5} className="m-3" />
      </div>
    );
  }

  if (data.length === 0) {
    return (
      <div className="bg-[#1c2128] border border-border rounded-lg p-6 text-center text-sm text-fg-muted">
        {emptyMessage}
      </div>
    );
  }

  return (
    <div className={classNames("overflow-x-auto bg-[#1c2128] border border-border rounded-lg", className)}>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border">
            {columns.map((col) => (
              <th
                key={col.key}
                className={classNames(
                  "sticky top-0 bg-[#1c2128] text-xs text-fg-muted font-medium uppercase tracking-wider px-3 py-2.5",
                  col.align === "right" && "text-right",
                  col.align === "center" && "text-center",
                  col.align === "left" && "text-left"
                )}
                style={col.width ? { width: col.width } : undefined}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((item, idx) => (
            <tr
              key={keyExtractor(item, idx)}
              onClick={() => onRowClick?.(item)}
              className={classNames(
                "border-b border-border/50 transition-colors duration-150",
                onRowClick && "cursor-pointer",
                selectedKey === keyExtractor(item, idx) ? "bg-accent-muted" : "hover:bg-[#21262d]"
              )}
            >
              {columns.map((col) => (
                <td
                  key={col.key}
                  className={classNames(
                    "px-3 py-2 text-[#e6edf3]",
                    col.align === "right" && "text-right num",
                    col.align === "center" && "text-center"
                  )}
                >
                  {col.render(item, idx)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
