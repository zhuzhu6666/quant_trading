type QueryErrorState = {
  isError: boolean;
  isRefetchError: boolean;
  error: unknown;
};

export function QueryErrorList({
  queries,
}: {
  queries: Array<{ label: string; query: QueryErrorState }>;
}) {
  const failures = queries.filter(({ query }) => query.isError || query.isRefetchError);

  return (
    <ul className="error-list" role="alert">
      {failures.map(({ label, query }) => {
        const message = query.error instanceof Error ? query.error.message : "请求失败";
        const phase = query.isRefetchError ? "刷新失败，当前显示缓存数据" : "请求失败";
        return <li key={label}>{label}：{phase} · {message}</li>;
      })}
    </ul>
  );
}
