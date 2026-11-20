"""Email every active client their weekly lead report.

Usage (cron / Render cron job, Mondays 09:00 UTC):
    python -m scripts.send_weekly_reports
"""
import asyncio
import logging

from app.services.weekly_report import send_weekly_reports


async def _main() -> None:
    result = await send_weekly_reports()
    logging.getLogger(__name__).info("weekly reports: %s", result)
    print(result)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
