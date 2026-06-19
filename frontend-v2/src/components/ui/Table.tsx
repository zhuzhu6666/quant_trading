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
  variant?: "default" | "compact";
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
  variant = "default",
}: TableProps<T>) {
  if (loading) {
    return (
      <div className="bg-white rounded-2xl shadow-card overflow-hidden">
        <Skeleton variant="table-row" count={5} className="m-4" />
      </div>
    );
  }

  if (data.length === 0) {
    return (
      <div className="bg-white rounded-2xl shadow-card p-8 text-center text-sm text-text-secondary">
        {emptyMessage}
      </div>
    );
  }

  return (
    <div className={classNames("overflow-x-auto bg-white rounded-2xl shadow-card", className)}>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-apple-divider">
            {columns.map((col) => (
              <th
                key={col.key}
                className={classNames(
                  "sticky top-0 bg-white text-2xs text-text-secondary font-medium uppercase tracking-wider px-4 py-3",
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
                "border-b border-apple-divider transition-colors duration-200",
                onRowClick && "cursor-pointer",
                selectedKey === keyExtractor(item, idx)
                  ? "bg-accent-lighter"
                  : "hover:bg-apple-bg"
              )}
            >
              {columns.map((col) => (
                <td
                  key={col.key}
                  className={classNames(
                    "px-4 py-3 text-text-primary",
                    col.align === "right" && "text-right num",
                    col.align === "center" && "text-center",
                    variant === "compact" && "py-2"
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
