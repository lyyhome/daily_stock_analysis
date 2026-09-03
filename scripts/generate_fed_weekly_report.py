"""Generate the weekly Federal Reserve agenda and market-impact report."""

from src.config import get_config, setup_env
from src.core.market_review_runtime import build_market_review_runtime
from src.services.fed_weekly_report import generate_fed_weekly_report


if __name__ == "__main__":
    setup_env()
    config = get_config()
    notifier, analyzer, search_service = build_market_review_runtime(config)
    path = generate_fed_weekly_report(
        notifier=notifier,
        search_service=search_service,
        analyzer=analyzer,
    )
    print(f"Fed weekly report saved: {path}")
