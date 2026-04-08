# config.py — Toute la configuration vient du fichier .env
# Ne jamais écrire de valeurs sensibles directement ici.

from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # ── Connexion TWS ─────────────────────────────────────
    TWS_HOST:      str   = "127.0.0.1"   # même machine → toujours localhost
    TWS_PORT:      int   = 7496          # 7496 = compte réel | 7497 = paper
    TWS_CLIENT_ID: int   = 1

    # ── Votre numéro de compte IBKR (commence par U) ──────
    IBKR_ACCOUNT:  str = ""              # à renseigner dans .env

    # ── Limites de risque (argent réel) ───────────────────
    MAX_DRAWDOWN_PCT:  float = 5.0       # kill auto si drawdown ≥ 5%
    MAX_POSITION_PCT:  float = 2.0       # taille max par trade (% du capital)
    DAILY_LOSS_LIMIT:  float = 10000.0   # perte journalière max en €

    # ── Social / Twitter API ──────────────────────────────
    TWITTER_BEARER_TOKEN: str = ""         # optionnel — laisser vide pour RSS fallback

    # ── Serveur API ───────────────────────────────────────
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    class Config:
        env_file          = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
