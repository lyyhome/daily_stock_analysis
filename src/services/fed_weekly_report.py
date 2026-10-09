"""Federal Reserve weekly agenda and market-impact report generation."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

FED_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_NEWS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
EASTMONEY_US_MACRO_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EASTMONEY_US_MACRO_INDICATORS = {
    "cpi": "EMG00000733",
    "unemployment": "EMG00001039",
    "nonfarm": "EMG00152118",
}
EASTMONEY_DXY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_TREASURY_URL = "https://datacenter.eastmoney.com/api/data/get"
BEA_PIO_URL = "https://www.bea.gov/news/{year}/personal-income-and-outlays-{month}-{year}"


class _HTMLTextExtractor(HTMLParser):
    """Extract readable text from a public release page without extra dependencies."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def get_text(self) -> str:
        return " ".join(self.parts)


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
    def _as_date(value: Any) -> Optional[date]:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None

    @staticmethod
    def _as_number(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number else None

    @staticmethod
    def _month_offset(value: date, months: int) -> date:
        month_index = value.year * 12 + value.month - 1 + months
        return date(month_index // 12, month_index % 12 + 1, 1)

    def _fetch_eastmoney_indicator(
        self,
        indicator_id: str,
        *,
        as_of: date,
    ) -> Optional[Dict[str, Any]]:
        response = requests.get(
            EASTMONEY_US_MACRO_URL,
            params={
                "reportName": "RPT_ECONOMICVALUE_USA",
                "columns": "ALL",
                "filter": f'(INDICATOR_ID="{indicator_id}")',
                "sortColumns": "REPORT_DATE",
                "sortTypes": "-1",
                "pageNumber": "1",
                "pageSize": "12",
                "source": "WEB",
                "client": "WEB",
            },
            timeout=self.timeout_seconds,
            headers={"User-Agent": "Mozilla/5.0 daily-stock-analysis/1.0"},
        )
        response.raise_for_status()
        rows = (response.json().get("result") or {}).get("data") or []
        candidates: List[Dict[str, Any]] = []
        for row in rows:
            period = self._as_date(row.get("REPORT_DATE"))
            release_date = self._as_date(row.get("PUBLISH_DATE"))
            value = self._as_number(row.get("VALUE"))
            if period is None or value is None or period > as_of:
                continue
            if release_date is not None and release_date > as_of:
                continue
            candidates.append(
                {
                    "period": period,
                    "released": release_date,
                    "value": value,
                    "previous": self._as_number(row.get("PRE_VALUE")),
                    "source_url": (
                        "https://www.bls.gov/cpi/"
                        if indicator_id == EASTMONEY_US_MACRO_INDICATORS["cpi"]
                        else "https://www.bls.gov/news.release/empsit.htm"
                    ),
                }
            )
        return max(candidates, key=lambda item: item["period"]) if candidates else None

    def _fetch_bea_release(self, period: date) -> Optional[Dict[str, Any]]:
        month = period.strftime("%B").lower()
        url = BEA_PIO_URL.format(year=period.year, month=month)
        response = requests.get(
            url,
            timeout=self.timeout_seconds,
            headers={"User-Agent": "Mozilla/5.0 daily-stock-analysis/1.0"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        parser = _HTMLTextExtractor()
        parser.feed(response.text)
        text = re.sub(r"\s+", " ", parser.get_text())
        title = f"Personal Income and Outlays, {period.strftime('%B %Y')}"
        if title not in text:
            return None

        monthly_match = re.search(
            r"From the preceding month,.*?Excluding food and energy, the PCE price index "
            r"(?:also )?increased\s+([\d.]+)\s+percent",
            text,
            re.IGNORECASE,
        )
        annual_match = re.search(
            r"From the same month one year ago,.*?Excluding food and energy, the PCE price index "
            r"increased\s+([\d.]+)\s+percent from one year ago",
            text,
            re.IGNORECASE,
        )
        if not monthly_match or not annual_match:
            return None

        release_match = re.search(
            r"EMBARGOED UNTIL RELEASE AT.*?([A-Z][a-z]+\s+\d{1,2},\s+20\d{2})",
            text,
            re.IGNORECASE,
        )
        released = None
        if release_match:
            try:
                released = datetime.strptime(release_match.group(1), "%B %d, %Y").date()
            except ValueError:
                pass
        return {
            "period": period,
            "released": released,
            "monthly": float(monthly_match.group(1)),
            "value": float(annual_match.group(1)),
            "source_url": url,
        }

    def _fetch_latest_bea_core_pce(self, *, as_of: date) -> Optional[Dict[str, Any]]:
        latest = None
        for offset in range(1, 5):
            period = self._month_offset(as_of, -offset)
            try:
                latest = self._fetch_bea_release(period)
            except Exception as exc:
                logger.warning("BEA release unavailable for %s: %s", period, exc)
                continue
            if latest:
                break
        if latest is None:
            return None

        previous_period = self._month_offset(latest["period"], -1)
        try:
            previous_release = self._fetch_bea_release(previous_period)
        except Exception as exc:
            logger.warning("Previous BEA release unavailable for %s: %s", previous_period, exc)
            previous_release = None
        latest["previous"] = previous_release.get("value") if previous_release else None
        latest["previous_monthly"] = previous_release.get("monthly") if previous_release else None
        return latest

    def _fetch_eastmoney_market_history(
        self,
        *,
        as_of: date,
    ) -> Dict[str, List[Tuple[date, float]]]:
        result: Dict[str, List[Tuple[date, float]]] = {"dxy": [], "us10y": []}
        params = {
            "secid": "100.UDI",
            "klt": "101",
            "fqt": "1",
            "lmt": "12",
            "end": "20500000",
            "iscca": "1",
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64",
            "ut": "f057cbcbce2a86e2866ab8877db1d059",
            "forcect": "1",
        }
        try:
            response = requests.get(
                EASTMONEY_DXY_URL,
                params=params,
                timeout=self.timeout_seconds,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            data = response.json().get("data") or {}
            for row in data.get("klines") or []:
                fields = row.split(",")
                if len(fields) < 3:
                    continue
                observation_date = self._as_date(fields[0])
                value = self._as_number(fields[2])
                if observation_date and value is not None and observation_date <= as_of:
                    result["dxy"].append((observation_date, value))
            result["dxy"].sort(key=lambda item: item[0])
        except Exception as exc:
            logger.warning("EastMoney DXY history unavailable: %s", exc)

        treasury_params = {
            "type": "RPTA_WEB_TREASURYYIELD",
            "sty": "ALL",
            "st": "SOLAR_DATE",
            "sr": "-1",
            "token": "894050c76af8597a853f5b408b759f5d",
            "p": "1",
            "ps": "20",
            "pageNo": "1",
            "pageNum": "1",
        }
        try:
            treasury_response = requests.get(
                EASTMONEY_TREASURY_URL,
                params=treasury_params,
                timeout=self.timeout_seconds,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            treasury_response.raise_for_status()
            treasury_rows = (treasury_response.json().get("result") or {}).get("data") or []
            for row in treasury_rows:
                observation_date = self._as_date(row.get("SOLAR_DATE"))
                value = self._as_number(row.get("EMG00001310"))
                if observation_date and value is not None and observation_date <= as_of:
                    result["us10y"].append((observation_date, value))
            result["us10y"].sort(key=lambda item: item[0])
        except Exception as exc:
            logger.warning("EastMoney Treasury history unavailable: %s", exc)
        return result

    def _fetch_macro_snapshot(self, *, as_of: date) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {}
        for key, indicator_id in EASTMONEY_US_MACRO_INDICATORS.items():
            try:
                observation = self._fetch_eastmoney_indicator(indicator_id, as_of=as_of)
                if observation is not None:
                    snapshot[key] = observation
            except Exception as exc:
                logger.warning("EastMoney %s observation unavailable: %s", key, exc)
        try:
            core_pce = self._fetch_latest_bea_core_pce(as_of=as_of)
            if core_pce is not None:
                snapshot["core_pce"] = core_pce
        except Exception as exc:
            logger.warning("BEA core PCE observation unavailable: %s", exc)
        try:
            snapshot.update(self._fetch_eastmoney_market_history(as_of=as_of))
        except Exception as exc:
            logger.warning("EastMoney DXY/Treasury observations unavailable: %s", exc)
        return snapshot

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
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Explain real releases and market moves in plain language."""
        lines: List[str] = []
        cpi = snapshot.get("cpi")
        core_pce = snapshot.get("core_pce")
        nonfarm = snapshot.get("nonfarm")
        unemployment = snapshot.get("unemployment")
        dxy = snapshot.get("dxy") or []
        treasury = snapshot.get("us10y") or []

        def describe_change(current: Optional[float], previous: Optional[float], unit: str = "") -> str:
            if current is None or previous is None:
                return "前值不可用，暂无法比较方向"
            difference = current - previous
            if abs(difference) < 0.000001:
                return "与前值持平"
            direction = "上升" if difference > 0 else "下降"
            return f"较前值{direction} {abs(difference):.2f}{unit}"

        def observation_period(observation: Optional[Dict[str, Any]]) -> str:
            if not observation:
                return "最新数据暂不可用"
            period = observation.get("period")
            released = observation.get("released")
            release_text = f"，发布日 {released.isoformat()}" if released else ""
            source_url = observation.get("source_url", "")
            source = f"；[数据来源]({source_url})" if source_url else ""
            return f"数据期 {period:%Y-%m}{release_text}{source}" if period else source

        cpi_change: Optional[float] = None
        if cpi:
            cpi_value = cpi["value"]
            cpi_previous = cpi.get("previous")
            cpi_change = cpi_value - cpi_previous if cpi_previous is not None else None
            if cpi_change is not None and cpi_change > 0:
                cpi_meaning = "通胀重新走高，侵蚀家庭购买力并压缩美联储降息空间，对消费和估值不利。"
            elif cpi_change is not None and cpi_change < 0:
                cpi_meaning = "通胀回落有利于实际购买力，也增加未来降息空间，对经济和风险资产偏正面。"
            else:
                cpi_meaning = "通胀没有继续降温，若水平仍高于美联储目标，利率维持高位的风险仍在。"
            lines.append(
                f"- CPI：同比 {cpi_value:.2f}%，前值 {cpi_previous:.2f}%" if cpi_previous is not None
                else f"- CPI：同比 {cpi_value:.2f}%"
            )
            lines.append(f"  - 变化与含义：{describe_change(cpi_value, cpi_previous, ' 个百分点')}；{cpi_meaning}")
            lines.append(f"  - {observation_period(cpi)}")
        else:
            cpi_value = None
            cpi_meaning = ""
            lines.append("- CPI：当前数据源未返回可核验的最新公布值。")

        core_pce_change: Optional[float] = None
        core_pce_monthly_change: Optional[float] = None
        if core_pce:
            core_pce_value = core_pce["value"]
            core_pce_previous = core_pce.get("previous")
            core_pce_change = (
                core_pce_value - core_pce_previous
                if core_pce_previous is not None
                else None
            )
            monthly_value = core_pce.get("monthly")
            previous_monthly = core_pce.get("previous_monthly")
            if monthly_value is not None and previous_monthly is not None:
                core_pce_monthly_change = monthly_value - previous_monthly
            if core_pce_change is not None and core_pce_change > 0:
                pce_meaning = "核心服务价格压力加重，不利于通胀回到 2% 目标，支持更久的限制性利率。"
            elif core_pce_change is not None and core_pce_change < 0:
                pce_meaning = "核心通胀同比降温，减轻美联储继续收紧的压力；仍需观察月率是否同步放缓。"
            else:
                pce_meaning = "核心通胀同比持平，说明去通胀进展停滞，难以单凭此项支持快速降息。"
            monthly_text = f"，月率 {monthly_value:.2f}%" if monthly_value is not None else ""
            if monthly_value is not None and previous_monthly is not None:
                monthly_text += f"（前月 {previous_monthly:.2f}%）"
            lines.append(
                f"- 核心 PCE：同比 {core_pce_value:.2f}%"
                + (f"，前值 {core_pce_previous:.2f}%" if core_pce_previous is not None else "")
                + monthly_text
            )
            lines.append(
                f"  - 变化与含义：{describe_change(core_pce_value, core_pce_previous, ' 个百分点')}；{pce_meaning}"
            )
            lines.append(f"  - {observation_period(core_pce)}")
        else:
            core_pce_value = None
            pce_meaning = ""
            lines.append("- 核心 PCE：BEA 最新发布稿暂不可用，未以过期数据代替。")

        nonfarm_change: Optional[float] = None
        if nonfarm:
            nonfarm_value = nonfarm["value"]
            nonfarm_previous = nonfarm.get("previous")
            nonfarm_change = (
                nonfarm_value - nonfarm_previous
                if nonfarm_previous is not None
                else None
            )
            if nonfarm_change is not None and nonfarm_change < 0:
                labor_meaning = "新增岗位减少，说明招聘动能降温；这减轻工资通胀和利率压力，但过弱会伤害消费与企业盈利。"
            elif nonfarm_change is not None and nonfarm_change > 0:
                labor_meaning = "新增岗位增加，支撑收入与消费，但就业偏强也可能让工资和利率保持高位。"
            else:
                labor_meaning = "招聘动能与前值接近，对政策路径没有提供明显的新方向。"
            lines.append(
                f"- 非农就业：新增 {nonfarm_value:+.0f} 千人"
                + (f"，前值 {nonfarm_previous:+.0f} 千人" if nonfarm_previous is not None else "")
            )
            lines.append(f"  - 变化与含义：{describe_change(nonfarm_value, nonfarm_previous, ' 千人')}；{labor_meaning}")
            lines.append(f"  - {observation_period(nonfarm)}")
        else:
            nonfarm_value = None
            labor_meaning = ""
            lines.append("- 非农就业：当前数据源未返回可核验的最新公布值。")

        unemployment_change: Optional[float] = None
        if unemployment:
            unemployment_value = unemployment["value"]
            unemployment_previous = unemployment.get("previous")
            unemployment_change = (
                unemployment_value - unemployment_previous
                if unemployment_previous is not None
                else None
            )
            if unemployment_change is not None and unemployment_change > 0:
                unemployment_meaning = "失业率上升说明劳动力市场转松，有助于压低工资通胀，但持续上升会提高衰退和盈利下修风险。"
            elif unemployment_change is not None and unemployment_change < 0:
                unemployment_meaning = "失业率下降表明就业仍有韧性，有利于收入但可能延后降息。"
            else:
                unemployment_meaning = "失业率持平，劳动力市场暂未显示明显恶化或再度过热。"
            lines.append(
                f"- 失业率：{unemployment_value:.2f}%"
                + (f"，前值 {unemployment_previous:.2f}%" if unemployment_previous is not None else "")
            )
            lines.append(
                f"  - 变化与含义：{describe_change(unemployment_value, unemployment_previous, ' 个百分点')}；{unemployment_meaning}"
            )
            lines.append(f"  - {observation_period(unemployment)}")
        else:
            unemployment_value = None
            unemployment_meaning = ""
            lines.append("- 失业率：当前数据源未返回可核验的最新公布值。")

        dxy_change: Optional[float] = None
        if dxy:
            latest_date, latest_dxy = dxy[-1]
            prior_dxy = dxy[-6][1] if len(dxy) >= 6 else (dxy[0][1] if len(dxy) >= 2 else None)
            if prior_dxy:
                dxy_change = (latest_dxy / prior_dxy - 1) * 100
            move = "上涨" if dxy_change is not None and dxy_change > 0.05 else "下跌" if dxy_change is not None and dxy_change < -0.05 else "基本持平"
            lines.append(
                f"- 美元指数 DXY：{latest_dxy:.2f}，近 {min(5, max(1, len(dxy) - 1))} 个交易日"
                + (f"{move} {abs(dxy_change):.2f}%" if dxy_change is not None else "变化暂不可比较")
                + f"（截至 {latest_date.isoformat()}；[东方财富美元指数](https://quote.eastmoney.com/gb/zsUDI.html)）。"
            )
        else:
            lines.append("- 美元指数 DXY：当前行情源未返回可核验的近期行情。")

        yield_change_bps: Optional[float] = None
        if treasury:
            latest_yield_date, latest_yield = treasury[-1]
            prior_yield = treasury[-6][1] if len(treasury) >= 6 else (treasury[0][1] if len(treasury) >= 2 else None)
            if prior_yield is not None:
                yield_change_bps = (latest_yield - prior_yield) * 100
            move = "上行" if yield_change_bps is not None and yield_change_bps > 1 else "下行" if yield_change_bps is not None and yield_change_bps < -1 else "基本持平"
            lines.append(
                f"- 美国 10 年期美债收益率：{latest_yield:.2f}%"
                + (f"，近 {min(5, max(1, len(treasury) - 1))} 个交易日{move} {abs(yield_change_bps):.0f} 个基点" if yield_change_bps is not None else "，近期变化暂不可比较")
                + f"（截至 {latest_yield_date.isoformat()}；[东方财富美债收益率](https://data.eastmoney.com/cjsj/zmgzsyl.html)）。"
            )
        else:
            lines.append("- 美国 10 年期美债收益率：当前行情源未返回可核验的近期行情。")

        labor_cooling = (
            (nonfarm_change is not None and nonfarm_change < 0)
            or (unemployment_change is not None and unemployment_change > 0)
        )
        inflation_falling = (
            (cpi_change is not None and cpi_change < 0)
            or (core_pce_change is not None and core_pce_change < 0)
        )
        inflation_sticky = (
            (cpi is not None and cpi["value"] >= 2.5)
            or (core_pce is not None and core_pce["value"] >= 2.5)
        )

        stock_factors: List[str] = []
        if cpi_change is not None:
            stock_factors.append("CPI 通胀回落" if cpi_change < 0 else "CPI 通胀持平" if cpi_change == 0 else "CPI 通胀上升")
        if core_pce_change is not None:
            stock_factors.append("核心 PCE 降温" if core_pce_change < 0 else "核心 PCE 持平" if core_pce_change == 0 else "核心 PCE 走高")
        if labor_cooling:
            stock_factors.append("就业降温，利率预期有支撑但经济和盈利风险上升")
        if yield_change_bps is not None:
            stock_factors.append("10 年期收益率回落，缓解估值压力" if yield_change_bps < 0 else "10 年期收益率上行，增加估值压力" if yield_change_bps > 0 else "10 年期收益率持平")
        stock_bias = "偏谨慎、板块分化"
        if labor_cooling and (inflation_falling or (yield_change_bps is not None and yield_change_bps < 0)):
            stock_bias = "短线偏利多估值，但中期需防就业走弱引发盈利下修"
        elif inflation_sticky and (yield_change_bps is not None and yield_change_bps > 0):
            stock_bias = "偏利空，成长股和高估值板块承压更大"
        elif inflation_falling and yield_change_bps is not None and yield_change_bps < 0 and not labor_cooling:
            stock_bias = "偏利多，贴现率敏感的成长板块更受益"
        stock_impact = (
            f"综合结论：股票市场{stock_bias}。"
            + ("；".join(stock_factors) + "。" if stock_factors else "缺少宏观/利率实测值，暂以谨慎为主。")
        )

        gold_factors: List[str] = []
        if dxy_change is not None:
            gold_factors.append("美元走弱支撑金价" if dxy_change < -0.05 else "美元走强压制金价" if dxy_change > 0.05 else "美元变化对金价中性")
        if yield_change_bps is not None:
            gold_factors.append("美债收益率回落降低持有黄金的机会成本" if yield_change_bps < -1 else "美债收益率上升提高持有黄金的机会成本" if yield_change_bps > 1 else "美债收益率变化对金价中性")
        if dxy_change is not None and yield_change_bps is not None:
            if dxy_change < -0.05 and yield_change_bps < -1:
                gold_bias = "偏利多"
            elif dxy_change > 0.05 and yield_change_bps > 1:
                gold_bias = "偏利空"
            else:
                gold_bias = "信号分化、偏震荡"
        elif dxy_change is not None:
            gold_bias = "美元因素偏利多" if dxy_change < -0.05 else "美元因素偏利空" if dxy_change > 0.05 else "美元因素中性"
        elif yield_change_bps is not None:
            gold_bias = "利率因素偏利多" if yield_change_bps < -1 else "利率因素偏利空" if yield_change_bps > 1 else "利率因素中性"
        else:
            gold_bias = "暂缺美元与利率行情，方向判断有限"
        gold_impact = f"综合结论：黄金{gold_bias}。" + ("；".join(gold_factors) + "。" if gold_factors else "")
        if inflation_falling:
            gold_impact += " 核心通胀降温有助于缓解实际利率压力，但避险需求仍可能改变短线方向。"
        else:
            gold_impact += " 以上按名义收益率和美元方向判断，需留意实际利率与避险需求。"

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
            snapshot = self._fetch_macro_snapshot(as_of=today)
        except Exception as exc:
            logger.warning("Macro observations unavailable: %s", exc)
            snapshot = {}
        macro_analysis = self._build_macro_analysis(snapshot)
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
