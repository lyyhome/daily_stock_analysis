"""Federal Reserve weekly agenda and market-impact report generation."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)

FED_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_NEWS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"


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

    @staticmethod
    def _fallback_report(events: Iterable[FedEvent], context: str, today: date) -> str:
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
            "## 三、政策路径与市场影响\n"
            "- 关键判断：如果通胀粘性持续、就业依然强劲，市场更容易押注更持久的高利率路径；若通胀回落加速或就业转弱，政策窗口会向降息靠近。\n"
            "- 美元/美债：偏鹰往往支撑美元、抬升短端收益率，且压制长期债券和成长股估值；偏鸽则相反。\n"
            "- 美股/黄金：如果政策会议释放更灵活表述，黄金和成长股往往更受益；如果表述强化“更高利率更久”，则风险偏好可能回落。\n"
            "- A股/港股：需同步检视美元流动性、北向资金、跨境套利和科技板块估值承压程度，而不是只看会议标题。\n\n"
            "## 四、后续观察清单\n"
            "- 关注 FOMC 声明、点阵图、主席发布会措辞，以及利率期货对下一次会议的定价变化。\n"
            "- 同步核对美国 CPI、核心 PCE、非农和失业率，避免仅凭会议标题判断方向。\n"
            "- 结合美元指数、10年期美债收益率、VIX 和大盘估值的联动变化，判断是否出现“政策利好转为真实风险偏好修正”的切换。\n\n"
            "## 五、决策参考框架\n"
            "- 当通胀明显回落且就业逐步放缓时，倾向于将利差压缩和风险资产修复视为更重要的交易信号。\n"
            "- 当政策语气仍偏紧且金融条件异常紧绷时，优先关注防守型配置和美元、短久期债券的相对强弱。\n\n"
            "> 本报告为信息整理与情景分析，不构成投资建议；当官方数据或可信媒体信息不可得时，已明确标注为前瞻判断并需后续复核。\n"
        )

    def generate(self, *, today: Optional[date] = None) -> str:
        today = today or datetime.utcnow().date()
        events = self.fetch_official_calendar(today=today)
        context = self._search_context()
        base = self._fallback_report(events, context, today)
        if self.analyzer is None or not getattr(self.analyzer, "is_available", lambda: False)():
            return base
        prompt = (
            "请基于以下事实生成中文专业金融周报，严格区分已确认事实与情景推演，包含："
            "本周美联储会议议程、政策变量、对美元/美债/美股/黄金/A股港股的传导路径、"
            "后续一周观察指标。不要编造议程。\n\n" + base
        )
        try:
            generated = self.analyzer.generate_text(prompt, max_tokens=2200, temperature=0.2)
            return generated.strip() if generated and generated.strip() else base
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
