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
        context_lines = context or "- 新闻搜索暂不可用，以下影响判断需在数据恢复后复核。"
        return (
            f"# 美联储一周重要会议议程与市场影响（{today.isoformat()}）\n\n"
            "## 一、本周官方议程\n"
            f"{event_lines}\n\n"
            "## 二、专业市场信息\n"
            f"{context_lines}\n\n"
            "## 三、可能的市场影响\n"
            "- 偏鹰信号（加息倾向、通胀粘性或缩表加速）通常利多美元和短端美债收益率，压制长久期成长股、黄金及高估值资产。\n"
            "- 偏鸽信号（降息预期、就业走弱或通胀回落）通常压低美元和美债收益率，支持美股成长板块、黄金及风险资产。\n"
            "- A股和港股主要通过美元流动性、北向/跨境资金风险偏好及科技成长估值间接传导，不能机械等同于单日涨跌。\n\n"
            "## 四、后续观察清单\n"
            "- 关注 FOMC 声明、点阵图、主席发布会措辞，以及利率期货对下一次会议的定价变化。\n"
            "- 同步核对美国 CPI、核心 PCE、非农和失业率，避免仅凭会议标题判断方向。\n\n"
            "> 本报告为信息整理与情景分析，不构成投资建议。官方数据不可达时已明确标注，恢复后应复核。\n"
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
    return notifier.save_report_to_file(
        report,
        f"fed_weekly_report_{datetime.utcnow():%Y%m%d}.md",
    )
