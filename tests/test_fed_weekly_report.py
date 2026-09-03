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
        report = service.generate(today=date(2026, 9, 3))
    assert "美联储一周重要会议议程" in report
    assert "美元" in report
    assert "不构成投资建议" in report


def test_generate_uses_search_context_and_llm_when_available():
    item = SimpleNamespace(title="FOMC outlook", snippet="Rates may stay restrictive", url="https://example.com")
    search = SimpleNamespace(is_available=True)
    search.search_topic_news = Mock(return_value=SimpleNamespace(results=[item]))
    analyzer = SimpleNamespace(is_available=lambda: True, generate_text=Mock(return_value="  # generated Fed report  "))
    service = FedWeeklyReportService(search_service=search, analyzer=analyzer)
    with patch.object(service, "fetch_official_calendar", return_value=[]):
        report = service.generate(today=date(2026, 9, 3))
    assert report == "# generated Fed report"
    search.search_topic_news.assert_called_once()
    analyzer.generate_text.assert_called_once()


def test_fetch_official_calendar_fails_open():
    service = FedWeeklyReportService()
    with patch("src.services.fed_weekly_report.requests.get", side_effect=TimeoutError("offline")):
        assert service.fetch_official_calendar(today=date(2026, 9, 3)) == []
