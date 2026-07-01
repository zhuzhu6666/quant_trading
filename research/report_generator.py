"""
Weekly comprehensive research report generator.

Collects data from multiple sources (factor health, attribution, ML performance,
position risk, environment stats, experiment tracker, factor library) and writes
a human-readable Chinese markdown report to ``data/reports/{date}.md``.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


class WeeklyReport:
    """
    Weekly comprehensive research report.

    Sections:
    1. Factor health summary (from factor_health.json)
    2. Factor attribution summary (from factor_attribution.json)
    3. ML model performance (drift detector + XGB accuracy)
    4. Position analysis (VaR, concentration, exposure)
    5. Weekly environment (regime, volatility stats)
    6. Experiment summary (from ExperimentTracker)
    7. Factor library evolution (from FactorLibrary)

    Output: markdown to ``output_dir/{date}.md``.

    Parameters
    ----------
    output_dir : str
        Directory where generated reports are saved. Default ``"data/reports"``.
    experiment_tracker : ExperimentTracker or None
        Optional tracker instance for querying this week's experiments.
    factor_library : FactorLibrary or None
        Optional factor library instance for importing evolution_story entries.
    """

    def __init__(
        self,
        output_dir: str = "data/reports",
        experiment_tracker: Optional["ExperimentTracker"] = None,
        factor_library: Optional["FactorLibrary"] = None,
    ):
        self._output_dir = Path(output_dir)
        self._tracker = experiment_tracker
        self._library = factor_library
        logger.debug(f"WeeklyReport initialised, output_dir={self._output_dir}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self) -> str:
        """
        Generate the full weekly report.

        1. Assembles all sections in order.
        2. Writes the markdown to ``{output_dir}/{date}.md`` (creates dir if
           missing).
        3. Returns the report markdown string.

        Returns
        -------
        str
            The complete report in markdown format.
        """
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        week_range = self._week_range(now)

        lines: list[str] = []
        lines.append(f"# 每周研究报告 — {date_str}")
        lines.append("")
        lines.append(f"> **报告周期**: {week_range}")
        lines.append(f"> **生成时间**: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        lines.append("")
        lines.append("---")
        lines.append("")

        # Section 1: Factor health
        logger.info("Generating section: factor health")
        lines.append("## 1️⃣ 因子健康度概览")
        lines.append("")
        lines.append(self._section_factor_health())
        lines.append("")

        # Section 2: Factor attribution
        logger.info("Generating section: factor attribution")
        lines.append("## 2️⃣ 因子归因分析")
        lines.append("")
        lines.append(self._section_attribution())
        lines.append("")

        # Section 3: ML model performance
        logger.info("Generating section: ML performance")
        lines.append("## 3️⃣ 机器学习模型表现")
        lines.append("")
        lines.append(self._section_ml_performance())
        lines.append("")

        # Section 4: Position risk
        logger.info("Generating section: position risk")
        lines.append("## 4️⃣ 持仓风险分析")
        lines.append("")
        lines.append(self._section_position_risk())
        lines.append("")

        # Section 5: Weekly environment
        logger.info("Generating section: weekly environment")
        lines.append("## 5️⃣ 本周市场环境")
        lines.append("")
        lines.append(self._section_environment())
        lines.append("")

        # Section 6: Experiments
        logger.info("Generating section: experiments")
        lines.append("## 6️⃣ 本周实验记录")
        lines.append("")
        lines.append(self._section_experiments())
        lines.append("")

        # Section 7: Factor library evolution
        logger.info("Generating section: factor library evolution")
        lines.append("## 7️⃣ 因子库演进动态")
        lines.append("")
        lines.append(self._section_factor_library())
        lines.append("")

        # Footer
        lines.append("---")
        lines.append("")
        lines.append(
            f"*报告由 WeeklyReport 自动生成 | {now.strftime('%Y-%m-%d %H:%M:%S')}*"
        )
        lines.append("")

        report = "\n".join(lines)

        # Write to file
        self._output_dir.mkdir(parents=True, exist_ok=True)
        report_path = self._output_dir / f"{date_str}.md"
        try:
            report_path.write_text(report, encoding="utf-8")
            logger.info(f"Weekly report written to {report_path}")
        except OSError as exc:
            logger.error(f"Failed to write report to {report_path}: {exc}")

        return report

    # ------------------------------------------------------------------
    # Section generators
    # ------------------------------------------------------------------

    def _section_factor_health(self) -> str:
        """
        Read factor health data from ``data/charts/factor_health.json`` and
        produce a summary table.
        """
        path = Path("data/charts/factor_health.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning(f"Factor health data not available: {exc}")
            return self._na("数据未就绪")

        return self._format_factor_health(data)

    def _section_attribution(self) -> str:
        """
        Read attribution data from PostgreSQL state store and produce a summary table.
        """
        try:
            from backend.core.db import get_state_pg_conn
            conn = get_state_pg_conn(read_only=True)
            try:
                rows = conn.execute(
                    "SELECT factor, data_json FROM attribution_snapshot"
                ).fetchall()
                data = {}
                for r in rows:
                    try:
                        data[r["factor"]] = json.loads(r["data_json"])
                    except (json.JSONDecodeError, TypeError):
                        continue
            finally:
                conn.close()
            if not data:
                return self._na("暂无归因数据")
        except Exception as exc:
            logger.warning(f"Attribution data not available: {exc}")
            return self._na("数据未就绪")

        return self._format_attribution(data)

    def _section_ml_performance(self) -> str:
        """
        Check if ML factor data exists (e.g. ``data/charts/ml_performance.json``)
        and summarise accuracy / drift metrics.
        """
        path = Path("data/charts/ml_performance.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning(f"ML performance data not available: {exc}")
            return self._na("数据未就绪")

        return self._format_ml_performance(data)

    def _section_position_risk(self) -> str:
        """
        Read VaR / position risk data from ``data/charts/position_risk.json``
        if it exists.
        """
        path = Path("data/charts/position_risk.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning(f"Position risk data not available: {exc}")
            return self._na("数据未就绪")

        return self._format_position_risk(data)

    def _section_environment(self) -> str:
        """
        Read environment / regime stats from ``data/charts/environment.json``
        if it exists.
        """
        path = Path("data/charts/environment.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.warning(f"Environment data not available: {exc}")
            return self._na("数据未就绪")

        return self._format_environment(data)

    def _section_experiments(self) -> str:
        """
        Query the ExperimentTracker for this week's experiments and summarise.

        If no tracker was provided, tries to create one with the default db
        path (``data/experiments.db``).  Falls back to N/A if that also fails
        or returns no data.
        """
        tracker = self._tracker
        if tracker is None:
            try:
                from research.experiment_tracker import ExperimentTracker

                tracker = ExperimentTracker(db_path="data/experiments.db")
            except Exception as exc:
                logger.warning(f"Cannot instantiate ExperimentTracker: {exc}")
                return self._na("实验跟踪器不可用")

        # Compute the start-of-week timestamp for filtering
        now = datetime.now(timezone.utc)
        week_start = self._week_start_utc(now)

        try:
            runs = tracker.query(limit=1000)
        except Exception as exc:
            logger.error(f"Failed to query experiments: {exc}")
            return self._na("查询实验记录失败")

        # Filter to this week
        this_week = [r for r in runs if r.timestamp >= week_start.timestamp()]

        if not this_week:
            return self._na("本周暂无实验记录")

        return self._format_experiments(this_week)

    def _section_factor_library(self) -> str:
        """
        Import evolution_story entries from the FactorLibrary and summarise
        any new or updated factors this week.

        If no library was provided, attempts to import ``FactorLibrary``
        from ``research.factor_library``.  Falls back to N/A gracefully.
        """
        library = self._library
        if library is None:
            try:
                from research.factor_library import FactorLibrary

                library = FactorLibrary()
            except (ImportError, Exception) as exc:
                logger.warning(f"Cannot load FactorLibrary: {exc}")
                return self._na("因子库不可用")

        try:
            if hasattr(library, "get_evolution_story"):
                stories = library.get_evolution_story()
            elif hasattr(library, "evolution_story"):
                stories = library.evolution_story
            else:
                logger.warning(
                    "FactorLibrary has no evolution_story attribute"
                )
                return self._na("因子库无演进记录")
        except Exception as exc:
            logger.warning(f"Failed to read evolution story: {exc}")
            return self._na("读取演进记录失败")

        if not stories:
            return self._na("本周暂无因子库更新")

        return self._format_evolution_story(stories)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _na(reason: str = "数据未就绪") -> str:
        """Return a standard N/A message for a section."""
        return f"> *（{reason}）*"

    @staticmethod
    def _week_range(now: datetime) -> str:
        """Return a human-readable week range string like 'Mon 09 - Sun 15'."""
        # Find the Monday of the week containing `now` (Monday=0 ... Sunday=6)
        monday = now - __import__("datetime").timedelta(
            days=now.weekday()
        )
        sunday = monday + __import__("datetime").timedelta(days=6)
        return f"{monday.strftime('%m-%d')} ~ {sunday.strftime('%m-%d')}"

    @staticmethod
    def _week_start_utc(now: datetime) -> datetime:
        """Return the UTC datetime of Monday 00:00:00 of the current week."""
        monday = now - __import__("datetime").timedelta(
            days=now.weekday()
        )
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)

    # ------------------------------------------------------------------
    # Data-formatting methods  (each returns a markdown string)
    # ------------------------------------------------------------------

    @staticmethod
    def _format_factor_health(data: dict) -> str:
        """Format factor health JSON into a markdown summary table."""
        lines: list[str] = []

        # Attempt to extract a list of factors
        factors = data if isinstance(data, list) else data.get("factors", [])
        if not factors:
            return WeeklyReport._na("因子健康数据为空")

        lines.append("| 因子 | 健康分 | 状态 | 说明 |")
        lines.append("|------|--------|------|------|")
        for fac in factors:
            name = fac.get("name", fac.get("factor", "?"))
            score = fac.get("score", fac.get("health", "—"))
            status = fac.get("status", "—")
            note = fac.get("note", fac.get("description", ""))
            lines.append(f"| {name} | {score} | {status} | {note} |")

        lines.append("")
        lines.append(
            f"*共 {len(factors)} 个因子，"
            f"数据来源: factor_health.json*"
        )
        return "\n".join(lines)

    @staticmethod
    def _format_attribution(data: dict) -> str:
        """Format attribution JSON into a markdown summary table."""
        lines: list[str] = []

        records = (
            data
            if isinstance(data, list)
            else data.get("attributions", data.get("factors", []))
        )
        if not records:
            return WeeklyReport._na("归因数据为空")

        lines.append("| 因子 | 贡献收益 | 贡献比率 | 累计贡献 |")
        lines.append("|------|----------|----------|----------|")
        for rec in records:
            name = rec.get("name", rec.get("factor", "?"))
            ret = rec.get("return", rec.get("contribution", "—"))
            ratio = rec.get("ratio", rec.get("contribution_ratio", "—"))
            cum = rec.get("cumulative", rec.get("cum_return", "—"))
            lines.append(f"| {name} | {ret} | {ratio} | {cum} |")

        return "\n".join(lines)

    @staticmethod
    def _format_ml_performance(data: dict) -> str:
        """Format ML performance JSON into a markdown summary."""
        lines: list[str] = []

        accuracy = data.get("accuracy", data.get("xgb_accuracy", "—"))
        drift = data.get("drift", data.get("drift_score", "—"))
        precision = data.get("precision", "—")
        recall = data.get("recall", "—")
        f1 = data.get("f1", data.get("f1_score", "—"))

        lines.append("| 指标 | 数值 |")
        lines.append("|------|------|")
        lines.append(f"| XGB 准确率 | {accuracy} |")
        lines.append(f"| 漂移分数 | {drift} |")
        if precision != "—":
            lines.append(f"| 精确率 | {precision} |")
            lines.append(f"| 召回率 | {recall} |")
            lines.append(f"| F1 分数 | {f1} |")

        # Additional details if present
        details = data.get("details", data.get("details", None))
        if details:
            lines.append("")
            lines.append("**详细说明**")
            lines.append("")
            lines.append(str(details))

        return "\n".join(lines)

    @staticmethod
    def _format_position_risk(data: dict) -> str:
        """Format position risk JSON into a markdown summary."""
        lines: list[str] = []

        var_95 = data.get("VaR_95", data.get("var_95", "—"))
        var_99 = data.get("VaR_99", data.get("var_99", "—"))
        concentration = data.get(
            "concentration", data.get("concentration_ratio", "—")
        )
        exposure = data.get("exposure", data.get("total_exposure", "—"))
        top_positions = data.get("top_positions", data.get("positions", []))

        lines.append("| 指标 | 数值 |")
        lines.append("|------|------|")
        lines.append(f"| VaR (95%) | {var_95} |")
        lines.append(f"| VaR (99%) | {var_99} |")
        lines.append(f"| 集中度 | {concentration} |")
        lines.append(f"| 总风险敞口 | {exposure} |")

        if top_positions and len(top_positions) > 0:
            lines.append("")
            lines.append("**前几大持仓**")
            lines.append("")
            lines.append("| 标的 | 权重 | 风险贡献 |")
            lines.append("|------|------|----------|")
            for pos in top_positions[:10]:
                name = pos.get("name", pos.get("symbol", "?"))
                weight = pos.get("weight", pos.get("allocation", "—"))
                risk = pos.get("risk", pos.get("risk_contribution", "—"))
                lines.append(f"| {name} | {weight} | {risk} |")

        return "\n".join(lines)

    @staticmethod
    def _format_environment(data: dict) -> str:
        """Format environment / regime JSON into a markdown summary."""
        lines: list[str] = []

        regime = data.get("regime", data.get("market_regime", "—"))
        volatility = data.get("volatility", data.get("weekly_volatility", "—"))
        hv = data.get("hv", data.get("historical_vol", "—"))
        trend = data.get("trend", data.get("market_trend", "—"))
        correlation = data.get("correlation", data.get("avg_correlation", "—"))

        lines.append("| 指标 | 数值 |")
        lines.append("|------|------|")
        lines.append(f"| 市场体制 | {regime} |")
        lines.append(f"| 周波动率 | {volatility} |")
        if hv != "—":
            lines.append(f"| 历史波动率 | {hv} |")
        if trend != "—":
            lines.append(f"| 市场趋势 | {trend} |")
        if correlation != "—":
            lines.append(f"| 平均相关性 | {correlation} |")

        extras = data.get("details", data.get("extra", None))
        if extras:
            lines.append("")
            lines.append("**补充信息**")
            lines.append("")
            lines.append(str(extras))

        return "\n".join(lines)

    @staticmethod
    def _format_experiments(
        runs: list["Experiment"],
    ) -> str:
        """Format a list of Experiment objects into a markdown table."""
        lines: list[str] = []
        lines.append(
            f"本周共记录 **{len(runs)}** 个实验。"
        )
        lines.append("")

        lines.append("| 实验ID | 类型 | 状态 | 时间 | 关键指标 |")
        lines.append("|--------|------|------|------|----------|")
        for r in runs:
            ts = datetime.fromtimestamp(r.timestamp, tz=timezone.utc).strftime(
                "%m-%d %H:%M"
            )
            metrics_str = ", ".join(
                f"{k}={v}" for k, v in list(r.metrics.items())[:3]
            )
            if len(r.metrics) > 3:
                metrics_str += " …"
            lines.append(
                f"| `{r.run_id[:8]}…` | {r.experiment_type} | {r.status} "
                f"| {ts} | {metrics_str or '—'} |"
            )

        return "\n".join(lines)

    @staticmethod
    def _format_evolution_story(
        stories: list[dict],
    ) -> str:
        """Format factor library evolution_story entries into a summary."""
        lines: list[str] = []
        lines.append(
            f"本周因子库共 **{len(stories)}** 条演进记录。"
        )
        lines.append("")

        lines.append("| 日期 | 因子 | 类型 | 描述 |")
        lines.append("|------|------|------|------|")
        for entry in stories:
            date = entry.get("date", entry.get("timestamp", "—"))
            factor = entry.get("factor", entry.get("name", "?"))
            etype = entry.get(
                "type", entry.get("event_type", "update")
            )
            desc = entry.get("description", entry.get("note", ""))
            lines.append(f"| {date} | {factor} | {etype} | {desc} |")

        return "\n".join(lines)
