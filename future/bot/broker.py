"""ProjectX Gateway session for the TopStep account.

Deliberately the same shape as `bot.broker.Broker` -- resolve an
instrument, read the account, place and amend orders, list positions -- so the
trade logic reads the same at both venues. What is behind it could not be more
different: MT5 is a local terminal that returns tuples, ProjectX is an HTTPS API
that returns JSON and pushes fills over SignalR.

Two things are load-bearing and easy to get wrong:

*   **Whole contracts.** MT5 rounds a lot to a step and carries on. There is no
    rounding here: 2.7 contracts is 2 contracts, and a risk that does not reach
    one contract is a trade that cannot be taken. `size_contracts` returns 0 and
    the caller must refuse rather than "round up just this once".
*   **Endpoints are unverified.** The paths below follow the ProjectX Gateway
    documentation, but nothing here has been run against a live TopStep key. Do
    not trust this file with `--live` until `verify_connection()` has been run
    against the demo gateway and every path it prints has been confirmed. Until
    then `dry_run` stays True, which is also the default.
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from engine.instrument import InstrumentSpec

from .settings import Settings


class ProjectXError(RuntimeError):
    """The gateway could not be reached, or refused what was asked of it."""


class OrderRejected(ProjectXError):
    """The gateway accepted the request but refused to carry out the order."""


# Order side and type, as the gateway numbers them.
SIDE_BUY, SIDE_SELL = 0, 1
TYPE_LIMIT, TYPE_MARKET, TYPE_STOP = 1, 2, 4


@dataclass(frozen=True)
class FuturesPosition:
    contract_id: str
    direction: int           # +1 long, -1 short
    contracts: int
    price_open: float
    profit: float
    opened_at: datetime


def credentials() -> tuple[str, str]:
    """Username and API key from the environment, never from a settings file.

    A checked-in credential is a credential that leaks. The forex broker reads
    MT5 logins the same way, and for the same reason.
    """
    user = os.getenv("FUT_PROJECTX_USERNAME", "").strip()
    key = os.getenv("FUT_PROJECTX_API_KEY", "").strip()
    if not user or not key:
        raise ProjectXError(
            "ProjectX credentials are missing. Set FUT_PROJECTX_USERNAME and "
            "FUT_PROJECTX_API_KEY in the environment."
        )
    return user, key


def size_contracts(settings: Settings, risk_dollars: float,
                   stop_distance_points: float) -> int:
    """Whole contracts that risk at most `risk_dollars` to the stop.

    Returns 0 when the risk does not reach a single contract. That is a real
    answer, not an error: at $200 of risk and a 60-point MNQ stop the position
    is 1 contract; at a 120-point stop it is 0, and the honest response is to
    skip the trade rather than to take one that risks $240.
    """
    if stop_distance_points <= 0 or risk_dollars <= 0:
        return 0
    per_contract = stop_distance_points * settings.value_per_point
    if per_contract <= 0:
        return 0
    raw = risk_dollars / per_contract
    step = max(1, int(settings.contract_step))
    contracts = int(math.floor(raw / step) * step)
    if contracts < settings.min_contracts:
        return 0
    return min(contracts, settings.max_contracts)


def risk_dollars_for(settings: Settings, contracts: int,
                     stop_distance_points: float) -> float:
    """What a position of this size actually risks, once rounded to contracts."""
    return contracts * stop_distance_points * settings.value_per_point


class Broker:
    """Thin, explicit ProjectX session. Raises rather than returning None."""

    def __init__(self, settings: Settings, dry_run: bool = True):
        self.settings = settings
        self.dry_run = dry_run
        self._token: str | None = None
        self._account_id: int = int(settings.account_id or 0)
        self._contract_id: str = settings.contract_id
        self._spec: InstrumentSpec | None = None
        # Spacing between writes, for the same reason the forex broker has one:
        # three legs sent back to back read as automated order flooding.
        self._last_write: float | None = None
        self._requests = 0

    # -- plumbing ---------------------------------------------------------
    @property
    def requests(self) -> int:
        return self._requests

    def _url(self, path: str) -> str:
        return f"{self.settings.projectx_base_url.rstrip('/')}/{path.lstrip('/')}"

    def _post(self, path: str, payload: dict, authenticated: bool = True) -> dict:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self._url(path), data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        if authenticated:
            request.add_header("Authorization", f"Bearer {self._require_token()}")
        self._requests += 1
        try:
            with urllib.request.urlopen(
                    request, timeout=self.settings.request_timeout_seconds) as response:
                answer = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as error:
            raise ProjectXError(f"{path} returned HTTP {error.code}: "
                                f"{error.read()[:200]!r}") from error
        except urllib.error.URLError as error:
            raise ProjectXError(f"{path} is unreachable: {error.reason}") from error
        except json.JSONDecodeError as error:
            raise ProjectXError(f"{path} returned a body that is not JSON") from error
        # The gateway reports failure in the body with HTTP 200, so a successful
        # request is not a successful call and both have to be checked.
        if answer.get("success") is False:
            raise ProjectXError(f"{path} refused: {answer.get('errorMessage') or answer}")
        return answer

    def _space_writes(self) -> None:
        gap = self.settings.write_spacing_seconds
        if gap <= 0 or self._last_write is None:
            self._last_write = time.monotonic()
            return
        waited = time.monotonic() - self._last_write
        if waited < gap:
            time.sleep(gap - waited)
        self._last_write = time.monotonic()

    def _require_token(self) -> str:
        if self._token is None:
            raise ProjectXError("not connected: call connect() first")
        return self._token

    # -- session ----------------------------------------------------------
    def connect(self) -> None:
        """Authenticate, then resolve the account and the contract."""
        user, key = credentials()
        answer = self._post("/api/Auth/loginKey",
                            {"userName": user, "apiKey": key}, authenticated=False)
        token = answer.get("token")
        if not token:
            raise ProjectXError("login succeeded but returned no token")
        self._token = str(token)
        if not self._account_id:
            self._account_id = self._resolve_account()
        if not self._contract_id:
            self._contract_id = self._resolve_contract()

    def _resolve_account(self) -> int:
        answer = self._post("/api/Account/search", {"onlyActiveAccounts": True})
        accounts = answer.get("accounts") or []
        if not accounts:
            raise ProjectXError("no active ProjectX account on this key")
        if len(accounts) > 1:
            names = ", ".join(str(a.get("id")) for a in accounts)
            raise ProjectXError(
                f"{len(accounts)} active accounts ({names}); set account_id so the "
                f"bot cannot trade the wrong one")
        return int(accounts[0]["id"])

    def _resolve_contract(self) -> str:
        """The front-month contract for the configured root.

        Roll is not automatic. If the search returns several expiries this
        refuses: silently moving from MNQZ25 to MNQH26 would change the
        instrument under an open position and under the measured edge.
        """
        answer = self._post("/api/Contract/search",
                            {"searchText": self.settings.contract_symbol, "live": True})
        contracts = answer.get("contracts") or []
        if not contracts:
            raise ProjectXError(f"no contract matches {self.settings.contract_symbol!r}")
        if len(contracts) > 1:
            names = ", ".join(str(c.get("id")) for c in contracts)
            raise ProjectXError(
                f"{self.settings.contract_symbol!r} matches {len(contracts)} contracts "
                f"({names}); set contract_id to pin the expiry")
        return str(contracts[0]["id"])

    @property
    def account_id(self) -> int:
        if not self._account_id:
            raise ProjectXError("account not resolved: call connect() first")
        return self._account_id

    @property
    def contract_id(self) -> str:
        if not self._contract_id:
            raise ProjectXError("contract not resolved: call connect() first")
        return self._contract_id

    def spec(self) -> InstrumentSpec:
        """The contract, described the way `engine.sizing` expects.

        Tick size and value come from settings rather than from the gateway: they
        are exchange facts that must match what the backtest priced, and a
        gateway field that silently differed would resize every position.
        """
        if self._spec is None:
            settings = self.settings
            self._spec = InstrumentSpec(
                name=self._contract_id or settings.contract_symbol,
                digits=max(0, -int(math.floor(math.log10(settings.tick_size)))),
                point=settings.tick_size,
                volume_min=float(settings.min_contracts),
                volume_max=float(settings.max_contracts),
                volume_step=float(settings.contract_step),
                value_per_point=settings.value_per_point,
                stops_level_points=0.0,
                filling=0,
            )
        return self._spec

    # -- reads ------------------------------------------------------------
    def account(self) -> dict:
        answer = self._post("/api/Account/search", {"onlyActiveAccounts": True})
        for account in answer.get("accounts") or []:
            if int(account.get("id", 0)) == self.account_id:
                return account
        raise ProjectXError(f"account {self.account_id} is no longer active")

    def balance(self) -> float:
        return float(self.account().get("balance", 0.0))

    def positions(self) -> list[FuturesPosition]:
        answer = self._post("/api/Position/searchOpen", {"accountId": self.account_id})
        found = []
        for raw in answer.get("positions") or []:
            size = int(raw.get("size", 0))
            if not size:
                continue
            stamp = raw.get("creationTimestamp")
            found.append(FuturesPosition(
                contract_id=str(raw.get("contractId", "")),
                direction=1 if size > 0 else -1,
                contracts=abs(size),
                price_open=float(raw.get("averagePrice", 0.0)),
                profit=float(raw.get("profitAndLoss") or 0.0),
                opened_at=(datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                           if stamp else datetime.now(timezone.utc)),
            ))
        return found

    # -- writes -----------------------------------------------------------
    def place_market(self, direction: int, contracts: int,
                     stop_price: float, take_profit: float) -> dict:
        """One market leg with its protective stop and target attached.

        The stop goes on with the entry, not after it. A futures position that
        exists for even a second without a stop can move further against a
        TopStep daily limit than the whole trade was allowed to risk.
        """
        if contracts < 1:
            raise OrderRejected("refusing to send an order for 0 contracts")
        if stop_price <= 0:
            raise OrderRejected("refusing to send an order without a stop")
        payload = {
            "accountId": self.account_id,
            "contractId": self.contract_id,
            "type": TYPE_MARKET,
            "side": SIDE_BUY if direction > 0 else SIDE_SELL,
            "size": int(contracts),
            "stopLossBracket": {"price": float(stop_price)},
            "takeProfitBracket": {"price": float(take_profit)} if take_profit else None,
        }
        if self.dry_run:
            return {"dry_run": True, "request": payload}
        self._space_writes()
        return self._post("/api/Order/place", payload)

    def amend_stop(self, order_id: int, stop_price: float) -> dict:
        payload = {"accountId": self.account_id, "orderId": int(order_id),
                   "stopPrice": float(stop_price)}
        if self.dry_run:
            return {"dry_run": True, "request": payload}
        self._space_writes()
        return self._post("/api/Order/modify", payload)

    def close_position(self, contracts: int | None = None) -> dict:
        payload = {"accountId": self.account_id, "contractId": self.contract_id}
        if contracts is not None:
            payload["size"] = int(contracts)
        if self.dry_run:
            return {"dry_run": True, "request": payload}
        self._space_writes()
        return self._post("/api/Position/closeContract", payload)

    # -- commissioning ----------------------------------------------------
    def verify_connection(self) -> dict:
        """Read-only smoke test to run before ever passing `--live`.

        Every write path in this file is unproven until this returns cleanly
        against the demo gateway and the account and contract it names are the
        intended ones.
        """
        self.connect()
        account = self.account()
        return {
            "base_url": self.settings.projectx_base_url,
            "account_id": self.account_id,
            "account_name": account.get("name"),
            "balance": float(account.get("balance", 0.0)),
            "contract_id": self.contract_id,
            "open_positions": len(self.positions()),
            "requests_used": self.requests,
            "dry_run": self.dry_run,
        }
