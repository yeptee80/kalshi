from enum import Enum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "kalshi.db"


class TradingMode(str, Enum):
    DEMO = "demo"
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kalshi_api_key_id: str = ""
    kalshi_private_key_path: Path = Path("~/.kalshi/kalshi-private-key.pem")
    # demo environment uses separate credentials (generated at demo.kalshi.co)
    kalshi_demo_api_key_id: str = ""
    kalshi_demo_private_key_path: Path = Path("~/.kalshi/keys/demo/demo-key.pem")

    def credentials_for(self, mode: "TradingMode") -> tuple[str, Path]:
        if mode == TradingMode.DEMO:
            return self.kalshi_demo_api_key_id, self.kalshi_demo_private_key_path
        return self.kalshi_api_key_id, self.kalshi_private_key_path

    odds_api_key: str = ""  # the-odds-api.com; free tier 500 credits/month

    trading_mode: TradingMode = TradingMode.PAPER
    live_trading_ack: str = "no"

    max_position_per_market: float = 24.0
    max_daily_loss: float = 100.0
    max_total_exposure: float = 80.0

    @property
    def live_trading_allowed(self) -> bool:
        return (
            self.trading_mode == TradingMode.LIVE
            and self.live_trading_ack.lower() == "yes"
        )


def get_settings() -> Settings:
    return Settings()
