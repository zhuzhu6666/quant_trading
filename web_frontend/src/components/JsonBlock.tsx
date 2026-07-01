import { compactJson } from "@/lib/compat";

export function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="json-block">{compactJson(value, 3000)}</pre>
  );
}
