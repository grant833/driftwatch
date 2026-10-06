from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv


def home() -> Path:
    return Path(os.getenv("DRIFTWATCH_HOME", Path.cwd()))


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_blocklist(text: str) -> frozenset[str]:
    tickers = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line:
            tickers.add(line)
    return frozenset(tickers)


@dataclass(frozen=True)
class Settings:
    database_url: str
    alpaca_key: str
    alpaca_secret: str
    sec_user_agent: str
    slack_webhook: str | None
    raw: dict
    blocklist: frozenset[str]

    @property
    def edgar(self) -> dict:
        return self.raw["edgar"]

    @property
    def gdelt(self) -> dict:
        return self.raw["gdelt"]


def load_settings() -> Settings:
    root = home()
    load_dotenv(root / ".env")
    raw = yaml.safe_load((root / "config" / "settings.yaml").read_text())
    blocklist = parse_blocklist((root / raw["universe"]["blocklist_file"]).read_text())

    ua = os.getenv("SEC_USER_AGENT", "").strip().strip('"')
    if "@" not in ua or "example.com" in ua:
        logging.getLogger(__name__).warning(
            "SEC_USER_AGENT should contain your real contact email; SEC may block requests."
        )

    return Settings(
        database_url=os.environ["DATABASE_URL"],
        alpaca_key=os.getenv("ALPACA_API_KEY", ""),
        alpaca_secret=os.getenv("ALPACA_API_SECRET", ""),
        sec_user_agent=ua,
        slack_webhook=os.getenv("SLACK_WEBHOOK_URL") or None,
        raw=raw,
        blocklist=blocklist,
    )
