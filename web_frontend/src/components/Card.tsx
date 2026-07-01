import { ReactNode } from "react";

export function MetricCard({
  title,
  children,
  className,
}: {
  title: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className || ""}`}>
      <h2>{title}</h2>
      <div>{children}</div>
    </section>
  );
}
