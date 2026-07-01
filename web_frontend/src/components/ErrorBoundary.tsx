import { Component, ErrorInfo, ReactNode } from "react";

type Props = {
  children: ReactNode;
};

type State = {
  error: Error | null;
};

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("页面渲染异常", error, info);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <section className="dashboard">
          <MetricFallback message={this.state.error.message || "页面渲染异常"} />
        </section>
      );
    }
    return this.props.children;
  }
}

function MetricFallback({ message }: { message: string }) {
  return (
    <div className="metric-card wide-panel">
      <div className="metric-title">页面异常</div>
      <p className="summary-note">{message}</p>
      <button className="action-btn action-ghost" type="button" onClick={() => window.location.reload()}>
        刷新页面
      </button>
    </div>
  );
}
