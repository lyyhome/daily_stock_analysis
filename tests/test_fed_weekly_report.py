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
        with patch.object(service, "_fetch_macro_snapshot", return_value={}):
            report = service.generate(today=date(2026, 9, 3))
    assert "美联储一周重要会议议程" in report
    assert "政策路径" in report
    assert "CPI" in report or "PCE" in report
    assert "非农就业" in report
    assert "失业率" in report
    assert "美元指数 DXY" in report
    assert "美国 10 年期美债收益率" in report
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
        with patch.object(service, "_fetch_macro_snapshot", return_value={}):
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


def test_fetch_eastmoney_indicator_uses_latest_published_nonempty_value():
    response = SimpleNamespace(
        raise_for_status=Mock(),
        json=lambda: {
            "result": {
                "data": [
                    {"REPORT_DATE": "2026-10-01 00:00:00", "PUBLISH_DATE": "2026-11-06", "VALUE": None, "PRE_VALUE": 29},
                    {"REPORT_DATE": "2026-09-01 00:00:00", "PUBLISH_DATE": "2026-10-02", "VALUE": 29, "PRE_VALUE": 133},
                    {"REPORT_DATE": "2026-08-01 00:00:00", "PUBLISH_DATE": "2026-09-04", "VALUE": 133, "PRE_VALUE": -10},
                ]
            }
        },
    )
    service = FedWeeklyReportService()
    with patch("src.services.fed_weekly_report.requests.get", return_value=response):
        observation = service._fetch_eastmoney_indicator(
            "EMG00152118",
            as_of=date(2026, 10, 9),
        )

    assert observation["period"] == date(2026, 9, 1)
    assert observation["released"] == date(2026, 10, 2)
    assert observation["value"] == 29
    assert observation["previous"] == 133


def test_parse_bea_core_pce_release_extracts_current_month_and_yoy_rates():
    html = """
    <h2>Personal Income and Outlays, August 2026</h2>
    <p>EMBARGOED UNTIL RELEASE AT 8:30 a.m. EDT, Wednesday, September 30, 2026 BEA 26-43</p>
    <p>From the preceding month, the PCE price index for August increased 0.3 percent.
    Excluding food and energy, the PCE price index increased 0.2 percent.</p>
    <p>From the same month one year ago, the PCE price index for August increased 3.4 percent.
    Excluding food and energy, the PCE price index increased 3.0 percent from one year ago.</p>
    """
    response = SimpleNamespace(status_code=200, text=html, raise_for_status=Mock())
    service = FedWeeklyReportService()
    with patch("src.services.fed_weekly_report.requests.get", return_value=response):
        observation = service._fetch_bea_release(date(2026, 8, 1))

    assert observation["period"] == date(2026, 8, 1)
    assert observation["released"] == date(2026, 9, 30)
    assert observation["monthly"] == 0.2
    assert observation["value"] == 3.0


def test_macro_analysis_calculates_values_and_separate_asset_impacts():
    market_dates = [date(2026, 10, day) for day in (2, 5, 6, 7, 8, 9)]
    snapshot = {
        "cpi": {"period": date(2026, 8, 1), "released": date(2026, 9, 11), "value": 3.4, "previous": 3.5},
        "core_pce": {
            "period": date(2026, 8, 1), "released": date(2026, 9, 30), "value": 3.0,
            "previous": 3.3, "monthly": 0.2, "previous_monthly": 0.2,
        },
        "nonfarm": {"period": date(2026, 9, 1), "released": date(2026, 10, 2), "value": 29, "previous": 133},
        "unemployment": {"period": date(2026, 9, 1), "released": date(2026, 10, 2), "value": 4.2, "previous": 4.1},
        "dxy": list(zip(market_dates, [102.3, 102.2, 102.1, 102.4, 102.12, 102.0])),
        "us10y": list(zip(market_dates, [5.30, 5.28, 5.27, 5.29, 5.28, 5.22])),
    }

    analysis = FedWeeklyReportService._build_macro_analysis(snapshot)
    report_block = analysis["report_block"]

    assert "CPI：同比 3.40%，前值 3.50%" in report_block
    assert "核心 PCE：同比 3.00%，前值 3.30%，月率 0.20%" in report_block
    assert "非农就业：新增 +29 千人，前值 +133 千人" in report_block
    assert "失业率：4.20%，前值 4.10%" in report_block
    assert "美元指数 DXY：102.00" in report_block
    assert "美国 10 年期美债收益率：5.22%" in report_block
    assert "通胀回落有利于实际购买力" in report_block
    assert "新增岗位减少" in report_block
    assert "股票市场短线偏利多估值" in report_block
    assert "黄金偏利多" in report_block


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
