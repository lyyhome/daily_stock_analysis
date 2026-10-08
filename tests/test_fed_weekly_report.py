from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.services.fed_weekly_report import FedWeeklyReportService


def test_parse_calendar_extracts_current_week_fomc_events():
    html = "<p>September 2-3, 2026 FOMC meeting and press conference</p>"
    events = FedWeeklyReportService._parse_calendar(html, today=date(2026, 9, 3))
    assert events
    assert events[0].date_text == "September 2-3, 2026"


def test_generate_is_available_without_network_or_llm():
    service = FedWeeklyReportService(search_service=None, analyzer=None)
    with patch.object(service, "fetch_official_calendar", return_value=[]):
        with patch.object(service, "_fetch_fred_observations", return_value={}):
            report = service.generate(today=date(2026, 9, 3))
    assert "美联储一周重要会议议程" in report
    assert "政策路径" in report
    assert "CPI" in report or "PCE" in report
    assert "非农就业" in report
    assert "失业率" in report
    assert "美元指数观察值" in report
    assert "美国 10 年期国债收益率" in report
    assert "股票市场：" in report
    assert "黄金：" in report
    assert "暂不可用" in report
    assert "美元" in report
    assert "不构成投资建议" in report


def test_generate_uses_search_context_and_llm_when_available():
    item = SimpleNamespace(title="FOMC outlook", snippet="Rates may stay restrictive", url="https://example.com")
    search = SimpleNamespace(is_available=True)
    search.search_topic_news = Mock(return_value=SimpleNamespace(results=[item]))
    analyzer = SimpleNamespace(is_available=lambda: True, generate_text=Mock(return_value="  # generated Fed report  "))
    service = FedWeeklyReportService(search_service=search, analyzer=analyzer)
    with patch.object(service, "fetch_official_calendar", return_value=[]):
        with patch.object(service, "_fetch_fred_observations", return_value={}):
            report = service.generate(today=date(2026, 9, 3))
    assert report.startswith("# generated Fed report")
    assert "## 三、美国宏观数据与市场变量" in report
    assert "## 四、基于数据的资产影响结论" in report
    search.search_topic_news.assert_called_once()
    analyzer.generate_text.assert_called_once()


def test_fetch_official_calendar_fails_open():
    service = FedWeeklyReportService()
    with patch("src.services.fed_weekly_report.requests.get", side_effect=TimeoutError("offline")):
        assert service.fetch_official_calendar(today=date(2026, 9, 3)) == []


def test_parse_fred_csv_skips_missing_observations():
    csv_text = (
        "observation_date,CPIAUCSL,PCEPILFE,PAYEMS,UNRATE,DTWEXBGS,DGS10\n"
        "2026-01-01,320.1,125.2,159000,4.1,120.5,4.2\n"
        "2026-02-01,.,125.7,159200,4.0,.,4.1\n"
    )
    response = SimpleNamespace(text=csv_text, raise_for_status=Mock())
    service = FedWeeklyReportService()
    with patch("src.services.fed_weekly_report.requests.get", return_value=response):
        observations = service._fetch_fred_observations()

    assert observations["CPIAUCSL"] == [(date(2026, 1, 1), 320.1)]
    assert observations["PCEPILFE"][-1] == (date(2026, 2, 1), 125.7)
    assert observations["DGS10"][-1] == (date(2026, 2, 1), 4.1)


def test_macro_analysis_calculates_values_and_separate_asset_impacts():
    dates = [date(2025 + (month - 1) // 12, (month - 1) % 12 + 1, 1) for month in range(1, 15)]
    observations = {
        "CPIAUCSL": list(zip(dates, [100.0 + month for month in range(12)] + [111.9, 112.0])),
        "PCEPILFE": list(zip(dates, [200.0 + 2 * month for month in range(12)] + [223.8, 224.0])),
        "PAYEMS": list(zip(dates, [100000.0 + 200 * month for month in range(14)])),
        "UNRATE": list(zip(dates, [4.0] * 14)),
        "DTWEXBGS": list(zip(dates, [130.0 - month for month in range(14)])),
        "DGS10": list(zip(dates, [5.0 - 0.1 * month for month in range(14)])),
    }

    analysis = FedWeeklyReportService._build_macro_analysis(observations)
    report_block = analysis["report_block"]

    assert "CPI：同比 10.89%" in report_block
    assert "核心 PCE：同比 10.89%" in report_block
    assert "最新月新增 +200 千人" in report_block
    assert "失业率：4.00%" in report_block
    assert "广义贸易加权美元指数" in report_block
    assert "不是 ICE DXY" in report_block
    assert "美国 10 年期国债收益率" in report_block
    assert "股票市场：实测通胀降温" in report_block
    assert "黄金：美元走弱且 10 年期收益率回落" in report_block


def test_generate_fed_weekly_report_sends_via_notifier_when_available():
    notifier = Mock()
    notifier.save_report_to_file.return_value = "D:/tmp/fed_report.md"
    notifier.send.return_value = True

    path = __import__("src.services.fed_weekly_report", fromlist=["generate_fed_weekly_report"]).generate_fed_weekly_report(
        notifier=notifier,
        search_service=None,
        analyzer=None,
    )

    assert path == "D:/tmp/fed_report.md"
    notifier.save_report_to_file.assert_called_once()
    notifier.send.assert_called_once_with(
        notifier.save_report_to_file.call_args.args[0],
        route_type="report",
        title="Federal Reserve Analysis Report",
    )
