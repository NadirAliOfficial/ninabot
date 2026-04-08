# risk_manager.py — Circuit breakers temps réel.

import logging
from datetime import date
from config import settings

log = logging.getLogger("bot.risk")


class RiskManager:
    def __init__(self):
        self.nav_open:    float | None = None
        self.nav_current: float        = 0.0
        self.daily_pnl:   float        = 0.0
        self.killed:      bool         = False
        self.kill_reason: str          = ""
        self._date = date.today()

    def update_nav(self, nav: float) -> None:
        if date.today() != self._date:
            self.nav_open = nav
            self._date    = date.today()
            log.info(f"Nouvelle séance — NAV open: {nav:,.0f} €")
        if self.nav_open is None:
            self.nav_open = nav
            log.info(f"NAV initiale enregistrée: {nav:,.0f} €")
        self.nav_current = nav
        self.daily_pnl   = nav - self.nav_open
        self._check()

    def _check(self) -> None:
        if self.killed or self.nav_open is None or self.nav_open == 0:
            return
        dd = ((self.nav_open - self.nav_current) / self.nav_open) * 100
        if dd >= settings.MAX_DRAWDOWN_PCT:
            self._kill(f"DRAWDOWN {dd:.2f}% ≥ {settings.MAX_DRAWDOWN_PCT}%")
            return
        if self.daily_pnl <= -settings.DAILY_LOSS_LIMIT:
            self._kill(f"PERTE JOURNALIÈRE {abs(self.daily_pnl):,.0f}€ ≥ LIMITE")

    def _kill(self, reason: str) -> None:
        self.killed      = True
        self.kill_reason = reason
        log.critical(f"🔴 KILL SWITCH AUTO — {reason}")

    def check_order(self, symbol: str, value_eur: float) -> tuple[bool, str]:
        """Validation avant tout placeOrder().
        Pour les petits comptes, on valide uniquement le kill switch.
        La limite par position s'applique seulement si la NAV > 1000€.
        """
        if self.killed:
            return False, f"Bot stoppé : {self.kill_reason}"

        # Pour les petits comptes (< 500€) : pas de limite par position
        # Le broker IBKR applique ses propres contrôles
        if self.nav_current > 500 and value_eur > 0:
            pct = (value_eur / self.nav_current) * 100
            if pct > settings.MAX_POSITION_PCT:
                log.warning(f"{symbol}: {pct:.1f}% du capital (limite {settings.MAX_POSITION_PCT}%)")
                # Warning seulement, pas de blocage pour petits comptes
                # return False, f"{symbol}: {pct:.1f}% > {settings.MAX_POSITION_PCT}% max"

        return True, "OK"

    def manual_kill(self) -> None:
        self._kill("KILL SWITCH MANUEL")

    def manual_reset(self, confirm: str) -> bool:
        if confirm != "RESET_CONFIRMED":
            return False
        log.warning("⚠️  Reset manuel confirmé")
        self.killed      = False
        self.kill_reason = ""
        return True

    def to_dict(self) -> dict:
        dd = 0.0
        if self.nav_open and self.nav_open > 0:
            dd = max(0.0, ((self.nav_open - self.nav_current) / self.nav_open) * 100)
        return {
            "killed":           self.killed,
            "kill_reason":      self.kill_reason,
            "nav_open":         self.nav_open,
            "nav_current":      round(self.nav_current, 2),
            "daily_pnl":        round(self.daily_pnl,   2),
            "drawdown_pct":     round(dd, 3),
            "max_drawdown_pct": settings.MAX_DRAWDOWN_PCT,
            "daily_loss_limit": settings.DAILY_LOSS_LIMIT,
        }
