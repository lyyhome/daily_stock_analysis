"""Federal Reserve weekly agenda and market-impact report generation."""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

FED_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_NEWS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_SERIES = {
    "CPIAUCSL": "CPI",
    "PCEPILFE": "Core PCE",
    "PAYEMS": "Nonfarm payrolls",
    "UNRATE": "Unemployment rate",
    "DTWEXBGS": "Broad trade-weighted U.S. dollar index",
    "DGS10": "10-year Treasury yield",
}


@dataclass(frozen=True)
class FedEvent:
    date_text: str
    title: str
    source_url: str = FED_CALENDAR_URL


class FedWeeklyReportService:
    """Build a source-aware weekly Fed agenda report with fail-open behavior."""

    def __init__(self, *, timeout_seconds: float = 8.0, search_service: Any = None, analyzer: Any = None):
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.search_service = search_service
        self.analyzer = analyzer

    def fetch_official_calendar(self, *, today: Optional[date] = None) -> List[FedEvent]:
        today = today or datetime.utcnow().date()
        try:
            response = requests.get(
                FED_CALENDAR_URL,
                timeout=self.timeout_seconds,
                headers={"User-Agent": "daily-stock-analysis/1.0"},
            )
            response.raise_for_status()
            return self._parse_calendar(response.text, today=today)
        except Exception as exc:
            logger.warning("Fed official calendar unavailable: %s", exc)
            return []

    @staticmethod
    def _parse_calendar(html: str, *, today: date) -> List[FedEvent]:
        events: List[FedEvent] = []
        seen = set()
        date_pattern = re.compile(
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:[-–]\d{1,2})?,?\s+20\d{2}",
            re.IGNORECASE,
        )
        for match in date_pattern.finditer(html):
            date_text = re.sub(r"\s+", " ", match.group(0)).strip()
            try:
                first_day = re.match(
                    r"(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})",
                    date_text,
                )
                year = re.search(r"20\d{2}", date_text)
                if first_day is None or year is None:
                    continue
                parsed = datetime.strptime(
                    f"{first_day.group('month')} {first_day.group('day')} {year.group(0)}",
                    "%B %d %Y",
                ).date()
            except ValueError:
                continue
            if parsed < today - timedelta(days=7) or parsed > today + timedelta(days=7):
                continue
            context = re.sub(r"\s+", " ", html[max(0, match.start() - 180): min(len(html), match.end() + 240)])
            title = "FOMC meeting / statement"
            if "minutes" in context.lower():
                title = "FOMC minutes"
            elif "press conference" in context.lower():
                title = "FOMC press conference"
            key = (date_text, title)
            if key not in seen:
                seen.add(key)
                events.append(FedEvent(date_text=date_text, title=title))
        return events

    def _search_context(self) -> str:
        if self.search_service is None or not getattr(self.search_service, "is_available", False):
            return ""
        try:
            response = self.search_service.search_topic_news(
                "Federal Reserve FOMC interest rates inflation markets",
                max_results=6,
                focus_keywords=["Federal Reserve", "FOMC", "interest rates", "markets", "inflation"],
            )
            results = getattr(response, "results", []) or []
            return "\n".join(
                f"- {item.title}: {item.snippet} ({item.url})"
                for item in results[:6]
            )
        except Exception as exc:
            logger.warning("Fed market context search unavailable: %s", exc)
            return ""

    def _fetch_fred_observations(self) -> Dict[str, List[Tuple[date, float]]]:
        """Fetch the latest official macroeconomic and market observations from FRED."""
        start_date = (date.today() - timedelta(days=550)).isoformat()
        response = requests.get(
            FRED_CSV_URL,
            params={"id": ",".join(FRED_SERIES), "cosd": start_date},
            timeout=self.timeout_seconds,
            headers={"User-Agent": "daily-stock-analysis/1.0"},
        )
        response.raise_for_status()
        observations: Dict[str, List[Tuple[date, float]]] = {
            series_id: [] for series_id in FRED_SERIES
        }
        reader = csv.DictReader(io.StringIO(response.text))
        for row in reader:
            date_text = row.get("observation_date") or row.get("DATE") or row.get("observation date")
            if not date_text:
                continue
            try:
                observation_date = date.fromisoformat(date_text.strip())
            except ValueError:
                continue
            for series_id in FRED_SERIES:
                raw_value = (row.get(series_id) or "").strip()
                if not raw_value or raw_value == ".":
                    continue
                try:
                    value = float(raw_value)
                except ValueError:
                    continue
                observations[series_id].append((observation_date, value))
        return observations

    @staticmethod
    def _latest_year_over_year(
        observations: List[Tuple[date, float]],
    ) -> Tuple[Optional[float], Optional[float]]:
        """Return latest YoY percent and its change from the previous monthly YoY."""
        if len(observations) < 2:
            return None, None
        by_month = {(item_date.year, item_date.month): value for item_date, value in observations}

        def calculate_yoy(item_date: date, value: float) -> Optional[float]:
            prior_value = by_month.get((item_date.year - 1, item_date.month))
            if prior_value is None or prior_value == 0:
                return None
            return (value / prior_value - 1) * 100

        current_date, current_value = observations[-1]
        previous_date, previous_value = observations[-2]
        current_yoy = calculate_yoy(current_date, current_value)
        previous_yoy = calculate_yoy(previous_date, previous_value)
        change = (
            current_yoy - previous_yoy
            if current_yoy is not None and previous_yoy is not None
            else None
        )
        return current_yoy, change

    @staticmethod
    def _format_percent_point_change(change: Optional[float]) -> str:
        if change is None:
            return "同比变化暂不可计算"
        return f"较上月 {change:+.2f} 个百分点"

    @staticmethod
    def _market_direction(change: Optional[float], threshold: float) -> Optional[str]:
        if change is None:
            return None
        if change > threshold:
            return "up"
        if change < -threshold:
            return "down"
        return "flat"

    @classmethod
    def _build_macro_analysis(
        cls,
        observations: Dict[str, List[Tuple[date, float]]],
    ) -> Dict[str, Any]:
        """Format verified data and derive distinct equity and gold implications."""
        lines: List[str] = []
        signals: Dict[str, Optional[str]] = {}

        for series_id, label in (("CPIAUCSL", "CPI"), ("PCEPILFE", "核心 PCE")):
            values = observations.get(series_id, [])
            yoy, yoy_change = cls._latest_year_over_year(values)
            if values and yoy is not None:
                latest_date = values[-1][0].strftime("%Y-%m")
                lines.append(
                    f"- {label}：同比 {yoy:.2f}%（数据期 {latest_date}，"
                    f"{cls._format_percent_point_change(yoy_change)}；"
                    f"[FRED {series_id}](https://fred.stlouisfed.org/series/{series_id})）"
                )
                signals[series_id] = cls._market_direction(yoy_change, 0.05)
            else:
                lines.append(
                    f"- {label}：最新同比数据暂不可用；未用估算值替代。"
                    f"[FRED {series_id}](https://fred.stlouisfed.org/series/{series_id})"
                )
                signals[series_id] = None

        payrolls = observations.get("PAYEMS", [])
        payroll_change: Optional[float] = None
        payroll_change_delta: Optional[float] = None
        if len(payrolls) >= 2:
            payroll_change = payrolls[-1][1] - payrolls[-2][1]
            if len(payrolls) >= 3:
                previous_payroll_change = payrolls[-2][1] - payrolls[-3][1]
                payroll_change_delta = payroll_change - previous_payroll_change
            lines.append(
                f"- 非农就业：最新月新增 {payroll_change:+.0f} 千人"
                f"（数据期 {payrolls[-1][0].strftime('%Y-%m')}；"
                f"[FRED PAYEMS](https://fred.stlouisfed.org/series/PAYEMS)）"
            )

            if payroll_change_delta is not None:
                lines.append(f"  - 较上月新增人数变化 {payroll_change_delta:+.0f} 千人。")
            signals["PAYEMS"] = cls._market_direction(payroll_change_delta, 50)
        else:
            lines.append(
                "- 非农就业：最新月新增人数暂不可用；未用估算值替代。"
                "[FRED PAYEMS](https://fred.stlouisfed.org/series/PAYEMS)"
            )
            signals["PAYEMS"] = None

        unemployment = observations.get("UNRATE", [])
        unemployment_change: Optional[float] = None
        if unemployment:
            latest_date, latest_rate = unemployment[-1]
            previous_rate = unemployment[-2][1] if len(unemployment) >= 2 else None
            if previous_rate is not None:
                unemployment_change = latest_rate - previous_rate
            previous_text = (
                f"，较上月 {unemployment_change:+.2f} 个百分点"
                if unemployment_change is not None
                else ""
            )
            lines.append(
                f"- 失业率：{latest_rate:.2f}%（数据期 {latest_date.strftime('%Y-%m')}"
                f"{previous_text}；[FRED UNRATE](https://fred.stlouisfed.org/series/UNRATE)）"
            )
        else:
            lines.append(
                "- 失业率：最新数据暂不可用；未用估算值替代。"
                "[FRED UNRATE](https://fred.stlouisfed.org/series/UNRATE)"
            )

        dollar = observations.get("DTWEXBGS", [])
        dollar_change: Optional[float] = None
        if dollar:
            latest_date, latest_value = dollar[-1]
            if len(dollar) >= 6 and dollar[-6][1] != 0:
                dollar_change = (latest_value / dollar[-6][1] - 1) * 100
            change_text = (
                f"，近 5 个交易日 {dollar_change:+.2f}%"
                if dollar_change is not None
                else "，近期变化暂不可计算"
            )
            lines.append(
                f"- 美元指数观察值：广义贸易加权美元指数 {latest_value:.2f}"
                f"（{latest_date.isoformat()}{change_text}；"
                f"[FRED DTWEXBGS](https://fred.stlouisfed.org/series/DTWEXBGS)）。"
                "此序列不是 ICE DXY。"
            )
        else:
            lines.append(
                "- 美元指数观察值：广义贸易加权美元指数暂不可用；此序列使用 FRED 指数，"
                "不是 ICE DXY。[FRED DTWEXBGS](https://fred.stlouisfed.org/series/DTWEXBGS)"
            )

        treasury = observations.get("DGS10", [])
        yield_change_bps: Optional[float] = None
        if treasury:
            latest_date, latest_yield = treasury[-1]
            if len(treasury) >= 6:
                yield_change_bps = (latest_yield - treasury[-6][1]) * 100
            change_text = (
                f"，近 5 个交易日 {yield_change_bps:+.0f} 个基点"
                if yield_change_bps is not None
                else "，近期变化暂不可计算"
            )
            lines.append(
                f"- 美国 10 年期国债收益率：{latest_yield:.2f}%"
                f"（{latest_date.isoformat()}{change_text}；"
                f"[FRED DGS10](https://fred.stlouisfed.org/series/DGS10)）。"
            )
        else:
            lines.append(
                "- 美国 10 年期国债收益率：最新数据暂不可用；未用估算值替代。"
                "[FRED DGS10](https://fred.stlouisfed.org/series/DGS10)"
            )

        inflation_directions = [
            signals.get("CPIAUCSL"),
            signals.get("PCEPILFE"),
        ]
        inflation_up = "up" in inflation_directions
        inflation_down = "down" in inflation_directions
        yield_direction = cls._market_direction(yield_change_bps, 10)
        dollar_direction = cls._market_direction(dollar_change, 0.3)
        labor_weak = (
            (payroll_change is not None and payroll_change < 100)
            or (unemployment_change is not None and unemployment_change >= 0.2)
        )
        labor_tight = (
            payroll_change is not None
            and payroll_change >= 150
            and unemployment_change is not None
            and unemployment_change <= 0
        )

        has_equity_inputs = any(
            value is not None
            for value in (
                *inflation_directions,
                signals.get("PAYEMS"),
                unemployment_change,
                yield_direction,
                dollar_direction,
            )
        )
        if not has_equity_inputs:
            stock_impact = (
                "本次未取得足够的 CPI、核心 PCE、就业、美元或利率实测数据，"
                "暂不对股市作方向性判断；通胀回落且收益率下行通常利于估值，"
                "通胀走热并伴随收益率上行则通常形成压力。"
            )
        elif (inflation_up or labor_tight) and (
            yield_direction == "up" or dollar_direction == "up"
        ):
            stock_impact = (
                "实测通胀/就业偏强且美元或长端收益率走高，整体对股票偏利空，"
                "高估值、长久期成长股对贴现率上升更敏感；需留意盈利预期能否抵消估值压力。"
            )
        elif inflation_down and yield_direction == "down" and not labor_weak:
            stock_impact = (
                "实测通胀降温且 10 年期收益率回落、就业未显著恶化，整体对股票偏利多，"
                "尤其有利于贴现率敏感的成长板块；仍需以企业盈利验证上涨持续性。"
            )
        elif labor_weak and yield_direction == "down":
            stock_impact = (
                "就业转弱与收益率下行同时出现：较低贴现率对估值有支撑，"
                "但非农/失业率恶化会提高盈利下修和衰退风险，股票总体偏谨慎、板块分化。"
            )
        else:
            stock_impact = (
                "通胀、就业、美元与收益率信号不完全一致，股票市场结论为中性偏震荡；"
                "成长股重点看 10 年期收益率，周期股重点看就业和盈利预期。"
            )

        has_gold_inputs = dollar_direction is not None or yield_direction is not None
        if not has_gold_inputs:
            gold_impact = (
                "美元指数和 10 年期收益率近期数据不足，暂不能给出基于实测值的方向判断；"
                "美元与利率同时回落通常利多黄金，同时上行通常构成压力。"
            )
        elif dollar_direction == "down" and yield_direction == "down":
            gold_impact = (
                "美元走弱且 10 年期收益率回落，对黄金形成双重支撑；"
                "但本报告使用名义收益率，黄金更直接受实际利率影响，结论需结合通胀保值债券实际收益率复核。"
            )
        elif dollar_direction == "up" and yield_direction == "up":
            gold_impact = (
                "美元走强且 10 年期收益率上行，对黄金构成双重压制；"
                "若避险需求显著增强，可能抵消部分压力。名义收益率不能替代实际利率。"
            )
        elif dollar_direction == "down" or yield_direction == "down":
            gold_impact = (
                "美元或 10 年期收益率至少一项走弱、另一项未同步走强，对黄金略偏支撑；"
                "信号存在分歧，宜观察实际利率与避险需求确认。"
            )
        elif dollar_direction == "up" or yield_direction == "up":
            gold_impact = (
                "美元或 10 年期收益率至少一项走强、另一项未同步走弱，对黄金略偏压制；"
                "信号存在分歧，宜观察实际利率与避险需求确认。"
            )
        else:
            gold_impact = "美元和 10 年期收益率近期大致持平，对黄金方向指引有限，需关注实际利率与避险需求。"

        report_block = "\n".join(
            [
                "## 三、美国宏观数据与市场变量",
                *lines,
                "",
                "## 四、基于数据的资产影响结论",
                f"- 股票市场：{stock_impact}",
                f"- 黄金：{gold_impact}",
            ]
        )
        return {
            "report_block": report_block,
            "stock_impact": stock_impact,
            "gold_impact": gold_impact,
        }

    @staticmethod
    def _fallback_report(
        events: Iterable[FedEvent],
        context: str,
        today: date,
        macro_report_block: str,
    ) -> str:
        event_lines = "\n".join(
            f"- {event.date_text}: {event.title}（来源：[Federal Reserve]({event.source_url})）"
            for event in events
        ) or "- 本周未从美联储官方日历识别到会议事项；请以官网最新日历为准。"

        fallback_context = (
            "- 政策路径：美联储本周的核心判断点仍然是通胀回落速度、就业韧性和金融条件变化，而不是单一会议标题。\n"
            "- 核心观察：重点关注 CPI、核心 PCE、非农就业、失业率、工资增速和反映信用状况的市场指标。\n"
            "- 传导逻辑：偏鹰信号通常利多美元和短端美债收益率，压制成长股、黄金及高估值资产；偏鸽信号则通常压低美元和收益率，支撑风险资产。\n"
            "- 资产配置：A股和港股主要通过美元流动性、北向资金、跨境资本风险偏好及科技成长估值间接传导，不应机械等同于单日涨跌。"
        )
        context_lines = context.strip() if context and context.strip() else fallback_context

        return (
            f"# 美联储一周重要会议议程与市场影响（{today.isoformat()}）\n\n"
            "## 一、本周官方议程\n"
            f"{event_lines}\n\n"
            "## 二、专业市场信息与前瞻判断\n"
            f"{context_lines}\n\n"
            f"{macro_report_block}\n\n"
            "## 五、后续观察清单\n"
            "- 关注 FOMC 声明、点阵图、主席发布会措辞，以及利率期货对下一次会议的定价变化。\n"
            "- 同步核对美国 CPI、核心 PCE、非农和失业率，避免仅凭会议标题判断方向。\n"
            "- 结合美元指数、10年期美债收益率、VIX 和大盘估值的联动变化，判断是否出现“政策利好转为真实风险偏好修正”的切换。\n\n"
            "## 六、决策参考框架\n"
            "- 当通胀明显回落且就业逐步放缓时，倾向于将利差压缩和风险资产修复视为更重要的交易信号。\n"
            "- 当政策语气仍偏紧且金融条件异常紧绷时，优先关注防守型配置和美元、短久期债券的相对强弱。\n\n"
            "> 本报告为信息整理与情景分析，不构成投资建议；当官方数据或可信媒体信息不可得时，已明确标注为前瞻判断并需后续复核。\n"
        )

    def generate(self, *, today: Optional[date] = None) -> str:
        today = today or datetime.utcnow().date()
        events = self.fetch_official_calendar(today=today)
        context = self._search_context()
        try:
            observations = self._fetch_fred_observations()
        except Exception as exc:
            logger.warning("FRED macro observations unavailable: %s", exc)
            observations = {}
        macro_analysis = self._build_macro_analysis(observations)
        macro_report_block = macro_analysis["report_block"]
        base = self._fallback_report(events, context, today, macro_report_block)
        if self.analyzer is None or not getattr(self.analyzer, "is_available", lambda: False)():
            return base
        prompt = (
            "请基于以下事实生成中文专业金融周报，严格区分已确认事实与情景推演，包含："
            "本周美联储会议议程、政策变量、对美元/美债/美股/黄金/A股港股的传导路径、"
            "后续一周观察指标。不要编造议程或数据，不得改动数据期、数值、方向判断和来源。"
            "文末必须原样保留‘美国宏观数据与市场变量’及‘基于数据的资产影响结论’区块。\n\n"
            + base
        )
        try:
            generated = self.analyzer.generate_text(prompt, max_tokens=2200, temperature=0.2)
            if not generated or not generated.strip():
                return base
            generated_report = generated.strip()
            if macro_report_block not in generated_report:
                generated_report = f"{generated_report}\n\n{macro_report_block}"
            return generated_report
        except Exception as exc:
            logger.warning("Fed report LLM generation unavailable: %s", exc)
            return base


def generate_fed_weekly_report(*, notifier: Any, search_service: Any = None, analyzer: Any = None) -> str:
    report = FedWeeklyReportService(search_service=search_service, analyzer=analyzer).generate()
    filepath = notifier.save_report_to_file(
        report,
        f"fed_weekly_report_{datetime.utcnow():%Y%m%d}.md",
    )
    if notifier is not None and hasattr(notifier, "send"):
        try:
            notifier.send(
                report,
                route_type="report",
                title="Federal Reserve Analysis Report",
            )
        except Exception as exc:
            logger.warning("Fed weekly report push failed: %s", exc)
    return filepath
