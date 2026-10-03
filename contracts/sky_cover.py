# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
SkyCover
========

Parametric flight-delay and cancellation insurance, underwritten by a
shared liquidity pool and settled by GenLayer validator consensus.

Liquidity providers deposit GEN into a pool and receive shares, the same
way GenLayer's own staking contracts track stake vs. shares. Travelers buy
a policy against a specific flight (airline/flight code + scheduled UTC
date), naming a payout amount and a delay threshold in minutes, and pay a
premium that is added to the pool. Once the flight date has passed,
*anyone* can call `resolve_policy`: GenLayer validators independently
fetch the flight's status from an admin-curated data source, extract a
structured outcome, and must agree on the same pay/no-pay decision before
a single unit of GEN moves. A cancelled flight, or a delay at or beyond
the policy's threshold, pays the policyholder out of the pool; anything
else releases the reserved capital back to the pool with no payout.

This is GenLayer's own headline example of a "judgment call" a plain
oracle can't make on its own: turning an ordinary status page into a
structured, agreed-upon fact. See
https://docs.genlayer.com/understand-genlayer-protocol/typical-use-cases
("Insurance -- parametric and evidence-based claims... Contracts fetch
weather data, flight statuses, or photographic evidence").

Design notes
------------
* Travelers never supply the URL that gets fetched. They pick from a small
  set of data sources the *admin* has registered (name + URL template with
  `{FLIGHT}`/`{DATE}` placeholders); the contract fills in the template
  itself. This closes off the obvious attack of a policyholder pointing
  the oracle at a server they control that always says "cancelled".
* The pay/no-pay decision is a single decision field, matched exactly
  between the leader and every validator (never inferred from a numeric
  field with a wide tolerance, which could let two validators agree "close
  enough" on minutes while actually disagreeing on which side of the
  threshold that puts the claim -- see "Pattern 1: Partial Field Matching"
  in GenLayer's Equivalence Principle docs). `delay_minutes` is compared
  too, but only as a secondary sanity check on top of the exact decision
  match, not as the thing that decides consensus.
* Underwriting math intentionally does not try to be a real actuarial
  model. `min_premium_bps` (premium must be worth at least this fraction
  of the payout) and `max_payout_multiple` (a payout can never be more
  than this many multiples of its own premium) are blunt but effective
  guards against a policy so mispriced it would only ever be rational to
  buy if you already knew the flight was doomed.
* `__receive__` is deliberately left undefined. GenVM's own dispatch rules
  then reject a bare GEN transfer with no method name outright (see
  Special Methods in the GenLayer docs), so the pool's `total_pool_balance`
  accounting can never silently drift from the contract's real on-chain
  balance via an unaccounted-for "donation".
* Both `get_wallet_policies` and the active-policy removal in
  `resolve_policy`/`expire_policy` are backed by auxiliary index maps
  (`wallet_policy_count`/`wallet_policy_index` and `active_policy_index`)
  instead of a linear scan, so their cost scales with one wallet's own
  history or O(1) respectively -- not with the platform's total policy
  count. See the two index-map sections below.
* This is not a licensed insurance product and carries no regulatory
  backing -- it is a demonstration of parametric, evidence-based
  settlement. Treat it accordingly; see the README for the same
  disclaimer GenLayer asks every adjudication dApp to carry.
"""

from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import html
import re
import typing

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ZERO_ADDRESS = Address("0x0000000000000000000000000000000000000000")

POLICY_ACTIVE = "ACTIVE"
POLICY_PAID = "PAID"
POLICY_DECLINED = "DECLINED"
POLICY_EXPIRED = "EXPIRED"

DECISION_PAY = "pay"
DECISION_NO_PAY = "no_pay"

FLIGHT_STATUS_ON_TIME = "on_time"
FLIGHT_STATUS_DELAYED = "delayed"
FLIGHT_STATUS_CANCELLED = "cancelled"
FLIGHT_STATUS_DIVERTED = "diverted"
FLIGHT_STATUS_UNKNOWN = "unknown"
VALID_FLIGHT_STATUSES = (
    FLIGHT_STATUS_ON_TIME,
    FLIGHT_STATUS_DELAYED,
    FLIGHT_STATUS_CANCELLED,
    FLIGHT_STATUS_DIVERTED,
    FLIGHT_STATUS_UNKNOWN,
)

# ASCII-only + fullmatch: `$` would otherwise accept a trailing "\n", and `\d`
# would accept non-ASCII digits (e.g. Arabic-Indic), both of which would end up
# verbatim in the fetched URL and the LLM prompt.
FLIGHT_CODE_RE = re.compile(r"[A-Z0-9\-]{2,10}", re.ASCII)
FLIGHT_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)

MAX_NAME_CHARS = 60
MAX_URL_TEMPLATE_CHARS = 300

MIN_LEAD_SECONDS = 6 * 3600            # policies must be bought >= 6h before the flight's UTC day starts
MAX_LEAD_SECONDS = 180 * 24 * 3600     # ...and no more than 180 days ahead
RESOLUTION_BUFFER_SECONDS = 30 * 3600  # wait 30h past the start of the UTC day before allowing resolution
MAX_CLAIM_WINDOW_SECONDS = 30 * 24 * 3600  # unresolved policies can be expired 30 days after that

MIN_DELAY_THRESHOLD_MINUTES = 30
MAX_DELAY_THRESHOLD_MINUTES = 24 * 60
DELAY_TOLERANCE_MINUTES = 15  # +/- minutes validators may diverge by on the secondary check

MAX_PROTOCOL_FEE_BPS = 2000       # 20% hard ceiling on the premium cut, regardless of admin
MAX_SINGLE_POLICY_EXPOSURE_BPS_CEILING = 5000  # admin can never let one policy exceed 50% of the pool

ERR_EXPECTED = "[EXPECTED]"  # deterministic, business-logic errors
ERR_LLM = "[LLM]"            # malformed / unfetchable oracle data


def _now() -> int:
    """Deterministic transaction-time Unix timestamp."""
    return int(datetime.now(timezone.utc).timestamp())


def _now_iso() -> str:
    """Deterministic transaction-time ISO 8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _parse_flight_date(flight_date: str) -> int:
    """Parses a validated 'YYYY-MM-DD' string into a UTC midnight timestamp."""
    if not FLIGHT_DATE_RE.fullmatch(flight_date):
        raise gl.vm.UserError(f"{ERR_EXPECTED} flight_date must look like YYYY-MM-DD")
    year, month, day = (int(part) for part in flight_date.split("-"))
    try:
        return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())
    except ValueError as exc:
        raise gl.vm.UserError(f"{ERR_EXPECTED} flight_date is not a real calendar date: {exc}")


MAX_PAGE_EXCERPT_CHARS = 6000


def _clean_page_text(raw_html: str) -> str:
    """Strips scripts, styles, comments and tags so the excerpt handed to the
    model is the page's visible text, not the first few KB of <head> markup."""
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", raw_html)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]*>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())[:MAX_PAGE_EXCERPT_CHARS]


def _clamp_minutes(value: typing.Any) -> int:
    try:
        minutes = int(round(float(value)))
    except (TypeError, ValueError):
        raise gl.vm.UserError(f"{ERR_LLM} non-numeric delay_minutes in oracle response: {value!r}")
    return max(0, min(48 * 60, minutes))


@gl.evm.contract_interface
class _Payee:
    """Typed stub used only to move GEN to an EOA (see Value Transfers docs)."""

    class View:
        pass

    class Write:
        pass


@allow_storage
@dataclass
class DataSource:
    id: u256
    name: str
    url_template: str  # must contain both {FLIGHT} and {DATE}
    active: bool
    added_by: Address
    added_at: str


@allow_storage
@dataclass
class Policy:
    id: u256
    policyholder: Address
    data_source_id: u256

    flight_code: str
    flight_date: str  # "YYYY-MM-DD", UTC
    flight_date_ts: u256

    premium: u256
    payout_amount: u256
    delay_threshold_minutes: u32

    purchased_at: str
    resolvable_at: u256
    claim_deadline: u256

    status: str
    decision: str
    observed_status: str
    observed_delay_minutes: u32
    verdict_summary: str
    resolved_at: str


@allow_storage
@dataclass
class AuditEntry:
    id: u256
    policy_id: u256
    actor: Address
    action: str
    detail: str
    timestamp: str


class SkyCover(gl.Contract):
    admin: Address
    treasury: Address
    paused: bool

    protocol_fee_bps: u32
    min_premium_bps: u32              # premium must be >= payout_amount * min_premium_bps / 10000
    max_payout_multiple: u32          # payout_amount must be <= premium * max_payout_multiple
    max_single_policy_exposure_bps: u32  # payout_amount must be <= pool_balance * this / 10000
    min_deposit: u256

    next_data_source_id: u256
    data_sources: TreeMap[str, DataSource]

    next_policy_id: u256
    policies: TreeMap[str, Policy]
    active_policy_ids: DynArray[u256]
    # policy_id (str) -> that policy's current index inside active_policy_ids,
    # so resolve_policy/expire_policy can remove it in O(1) instead of
    # scanning the whole array for its position.
    active_policy_index: TreeMap[str, u32]

    # wallet (as_hex) -> how many policies that wallet has ever bought, plus
    # "{wallet_as_hex}:{i}" -> policy_id for i in [0, count). Together these
    # let get_wallet_policies read in time proportional to *that wallet's*
    # own history instead of scanning every policy on the platform.
    wallet_policy_count: TreeMap[Address, u256]
    wallet_policy_index: TreeMap[str, u256]

    pool_shares: TreeMap[Address, u256]
    total_shares: u256
    total_pool_balance: u256
    total_reserved_exposure: u256

    total_premiums_collected: u256
    total_paid_out: u256
    total_policies_paid: u256
    total_policies_declined: u256
    total_policies_expired: u256

    next_audit_id: u256
    audit_log: DynArray[AuditEntry]

    # -----------------------------------------------------------------
    # constructor
    # -----------------------------------------------------------------

    def __init__(
        self,
        protocol_fee_bps: u32,
        min_premium_bps: u32,
        max_payout_multiple: u32,
        max_single_policy_exposure_bps: u32,
        min_deposit: u256,
    ):
        if protocol_fee_bps > MAX_PROTOCOL_FEE_BPS:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} protocol_fee_bps cannot exceed {MAX_PROTOCOL_FEE_BPS}"
            )
        if max_single_policy_exposure_bps == 0 or max_single_policy_exposure_bps > MAX_SINGLE_POLICY_EXPOSURE_BPS_CEILING:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} max_single_policy_exposure_bps must be between 1 and "
                f"{MAX_SINGLE_POLICY_EXPOSURE_BPS_CEILING}"
            )
        if min_premium_bps == 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} min_premium_bps must be positive")
        if max_payout_multiple == 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} max_payout_multiple must be positive")

        self.admin = gl.message.sender_address
        self.treasury = gl.message.sender_address
        self.paused = False

        self.protocol_fee_bps = protocol_fee_bps
        self.min_premium_bps = min_premium_bps
        self.max_payout_multiple = max_payout_multiple
        self.max_single_policy_exposure_bps = max_single_policy_exposure_bps
        self.min_deposit = min_deposit

        self.next_data_source_id = u256(1)
        self.next_policy_id = u256(1)
        self.total_shares = u256(0)
        self.total_pool_balance = u256(0)
        self.total_reserved_exposure = u256(0)
        self.total_premiums_collected = u256(0)
        self.total_paid_out = u256(0)
        self.total_policies_paid = u256(0)
        self.total_policies_declined = u256(0)
        self.total_policies_expired = u256(0)
        self.next_audit_id = u256(1)

    # -----------------------------------------------------------------
    # internal helpers
    # -----------------------------------------------------------------

    def _require_admin(self) -> None:
        if gl.message.sender_address != self.admin:
            raise gl.vm.UserError(f"{ERR_EXPECTED} caller is not the admin")

    def _get_policy(self, policy_id: u256) -> Policy:
        key = str(policy_id)
        if key not in self.policies:
            raise gl.vm.UserError(f"{ERR_EXPECTED} policy {policy_id} does not exist")
        return self.policies[key]

    def _get_data_source(self, data_source_id: u256) -> DataSource:
        key = str(data_source_id)
        if key not in self.data_sources:
            raise gl.vm.UserError(f"{ERR_EXPECTED} data source {data_source_id} does not exist")
        return self.data_sources[key]

    def _available_capacity(self) -> int:
        return int(self.total_pool_balance) - int(self.total_reserved_exposure)

    def _record(self, policy_id: u256, action: str, detail: str) -> None:
        entry = AuditEntry(
            id=self.next_audit_id,
            policy_id=policy_id,
            actor=gl.message.sender_address,
            action=action,
            detail=detail,
            timestamp=_now_iso(),
        )
        self.audit_log.append(entry)
        self.next_audit_id = u256(int(self.next_audit_id) + 1)

    def _add_active(self, policy_id: u256) -> None:
        self.active_policy_ids.append(policy_id)
        self.active_policy_index[str(policy_id)] = u32(len(self.active_policy_ids) - 1)

    def _remove_active(self, policy_id: u256) -> None:
        """O(1) swap-and-pop: looks the element's own index up directly
        instead of scanning active_policy_ids for it, and keeps
        active_policy_index in sync for whichever element gets moved."""
        key = str(policy_id)
        if key not in self.active_policy_index:
            return
        idx = int(self.active_policy_index[key])
        last = len(self.active_policy_ids) - 1
        if idx != last:
            moved_id = self.active_policy_ids[last]
            self.active_policy_ids[idx] = moved_id
            self.active_policy_index[str(moved_id)] = u32(idx)
        self.active_policy_ids.pop()
        del self.active_policy_index[key]

    def _track_wallet_policy(self, wallet: Address, policy_id: u256) -> None:
        idx = int(self.wallet_policy_count.get(wallet, u256(0)))
        self.wallet_policy_index[f"{wallet.as_hex}:{idx}"] = policy_id
        self.wallet_policy_count[wallet] = u256(idx + 1)

    def _pay(self, to: Address, amount: int) -> None:
        if amount <= 0:
            return
        if to == ZERO_ADDRESS:
            return
        _Payee(to).emit_transfer(value=u256(amount))

    # -----------------------------------------------------------------
    # liquidity pool
    # -----------------------------------------------------------------

    @gl.public.write.payable
    def deposit_liquidity(self) -> u256:
        if self.paused:
            raise gl.vm.UserError(f"{ERR_EXPECTED} the pool is paused")

        amount = int(gl.message.value)
        if amount < int(self.min_deposit):
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} deposit {amount} is below the minimum of {self.min_deposit}"
            )

        if int(self.total_shares) == 0:
            shares = amount
        else:
            pool_balance = int(self.total_pool_balance)
            if pool_balance == 0:
                # Every paid claim came out of a pool that still has
                # outstanding shares (fully wiped out, but old LPs haven't
                # all burned their now-worthless shares yet -- see
                # withdraw_liquidity). Minting fresh shares against a zero
                # balance would either divide by zero or silently let new
                # capital be diluted across dead shares, so new deposits
                # wait until the old shares are fully burned down to 0.
                raise gl.vm.UserError(
                    f"{ERR_EXPECTED} the pool was fully drained by paid claims and must reach "
                    f"0 outstanding shares (remaining LPs withdrawing) before new deposits "
                    f"are accepted"
                )
            shares = (amount * int(self.total_shares)) // pool_balance
        if shares <= 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} deposit too small to mint any shares")

        sender = gl.message.sender_address
        current = int(self.pool_shares.get(sender, u256(0)))
        self.pool_shares[sender] = u256(current + shares)
        self.total_shares = u256(int(self.total_shares) + shares)
        self.total_pool_balance = u256(int(self.total_pool_balance) + amount)

        self._record(u256(0), "LP_DEPOSIT", f"{sender.as_hex} deposited {amount}, minted {shares} shares")
        return u256(shares)

    @gl.public.write
    def withdraw_liquidity(self, shares: u256) -> u256:
        sender = gl.message.sender_address
        owned = int(self.pool_shares.get(sender, u256(0)))
        requested = int(shares)

        if requested <= 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} shares must be positive")
        if requested > owned:
            raise gl.vm.UserError(f"{ERR_EXPECTED} caller only owns {owned} shares")

        total_shares = int(self.total_shares)
        if int(self.total_reserved_exposure) > 0 and requested == total_shares:
            # A full exit would leave pool balance with no shares behind it,
            # orphaning the capital that still backs active policies.
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} the last shares cannot be withdrawn while active policies "
                f"still reserve capital"
            )

        # Exits are priced on the pool balance NET of capital reserved for
        # active policies, not the gross balance. Otherwise an LP who knows a
        # claim is about to pay (e.g. the flight is already cancelled) could
        # exit at full NAV before resolution and push the loss onto the LPs who
        # stay. The held-back portion remains in the pool and is released to
        # the remaining LPs when the policy resolves. Deposits stay priced on
        # the gross balance, so neither entering nor leaving can extract value
        # from the other LPs; the cost of moving is borne by the mover.
        net_pool_value = self._available_capacity()
        payout = (requested * net_pool_value) // total_shares
        if payout <= 0:
            if int(self.total_pool_balance) > 0:
                # The pool still holds capital (possibly all of it reserved for
                # active policies, which are NOT lost until they pay out) --
                # this caller's slice just rounds to 0 right now. Never burn
                # shares for nothing in that case: revert and let them retry.
                raise gl.vm.UserError(
                    f"{ERR_EXPECTED} nothing to withdraw: shares are worth 0 net of reserved capital"
                )
            # total_pool_balance == 0: paid claims wiped the pool out entirely,
            # so every remaining share is legitimately worthless. Let LPs
            # formally burn them for 0 GEN so total_shares can reach 0 and the
            # 1:1 bootstrap path in deposit_liquidity becomes available again.
            # (Deliberately keyed on the GROSS balance, not net-of-reserved:
            # "balance == reserved" is a normal, non-wiped-out state.)

        self.pool_shares[sender] = u256(owned - requested)
        self.total_shares = u256(int(self.total_shares) - requested)
        self.total_pool_balance = u256(int(self.total_pool_balance) - payout)

        self._record(u256(0), "LP_WITHDRAW", f"{sender.as_hex} burned {requested} shares for {payout}")
        self._pay(sender, payout)
        return u256(payout)

    # -----------------------------------------------------------------
    # data source registry (admin-curated fetch targets)
    # -----------------------------------------------------------------

    @gl.public.write
    def add_data_source(self, name: str, url_template: str) -> u256:
        self._require_admin()

        if len(name) == 0 or len(name) > MAX_NAME_CHARS:
            raise gl.vm.UserError(f"{ERR_EXPECTED} name must be 1-{MAX_NAME_CHARS} characters")
        if len(url_template) == 0 or len(url_template) > MAX_URL_TEMPLATE_CHARS:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} url_template must be 1-{MAX_URL_TEMPLATE_CHARS} characters"
            )
        if not (url_template.startswith("https://") or url_template.startswith("http://")):
            raise gl.vm.UserError(f"{ERR_EXPECTED} url_template must be http(s)")
        if "{FLIGHT}" not in url_template or "{DATE}" not in url_template:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} url_template must contain both {{FLIGHT}} and {{DATE}} placeholders"
            )

        source_id = self.next_data_source_id
        self.data_sources[str(source_id)] = DataSource(
            id=source_id,
            name=name,
            url_template=url_template,
            active=True,
            added_by=gl.message.sender_address,
            added_at=_now_iso(),
        )
        self.next_data_source_id = u256(int(source_id) + 1)
        self._record(u256(0), "DATA_SOURCE_ADDED", f"#{source_id} {name}")
        return source_id

    @gl.public.write
    def set_data_source_active(self, data_source_id: u256, active: bool) -> None:
        self._require_admin()
        source = self._get_data_source(data_source_id)
        source.active = active
        self._record(u256(0), "DATA_SOURCE_TOGGLED", f"#{data_source_id} active={active}")

    # -----------------------------------------------------------------
    # policy lifecycle
    # -----------------------------------------------------------------

    @gl.public.write.payable
    def buy_policy(
        self,
        data_source_id: u256,
        flight_code: str,
        flight_date: str,
        delay_threshold_minutes: u32,
        payout_amount: u256,
    ) -> u256:
        if self.paused:
            raise gl.vm.UserError(f"{ERR_EXPECTED} the pool is paused")

        source = self._get_data_source(data_source_id)
        if not source.active:
            raise gl.vm.UserError(f"{ERR_EXPECTED} that data source is no longer active")

        if not flight_code.isascii():
            raise gl.vm.UserError(f"{ERR_EXPECTED} flight_code must be ASCII")
        flight_code = flight_code.upper()
        if not FLIGHT_CODE_RE.fullmatch(flight_code):
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} flight_code must be 2-10 characters of A-Z, 0-9, or '-'"
            )

        flight_date_ts = _parse_flight_date(flight_date)
        now = _now()
        if flight_date_ts - now < MIN_LEAD_SECONDS:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} flight_date must be at least {MIN_LEAD_SECONDS // 3600}h out"
            )
        if flight_date_ts - now > MAX_LEAD_SECONDS:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} flight_date is more than {MAX_LEAD_SECONDS // 86400} days out"
            )

        threshold = int(delay_threshold_minutes)
        if threshold < MIN_DELAY_THRESHOLD_MINUTES or threshold > MAX_DELAY_THRESHOLD_MINUTES:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} delay_threshold_minutes must be between "
                f"{MIN_DELAY_THRESHOLD_MINUTES} and {MAX_DELAY_THRESHOLD_MINUTES}"
            )

        premium = int(gl.message.value)
        payout = int(payout_amount)
        if premium <= 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} premium must be greater than zero")
        if payout <= 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} payout_amount must be greater than zero")

        if payout > premium * int(self.max_payout_multiple):
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} payout_amount cannot exceed {self.max_payout_multiple}x the premium"
            )
        if premium * 10000 < payout * int(self.min_premium_bps):
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} premium is too low for the requested payout "
                f"(minimum {self.min_premium_bps} bps of payout_amount)"
            )
        if payout > self._available_capacity():
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} the pool doesn't have enough uncommitted capital for this payout "
                f"(available: {self._available_capacity()})"
            )
        if payout > (int(self.total_pool_balance) * int(self.max_single_policy_exposure_bps)) // 10000:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} payout_amount exceeds the per-policy concentration limit "
                f"({self.max_single_policy_exposure_bps} bps of the pool)"
            )

        fee = (premium * int(self.protocol_fee_bps)) // 10000
        net_premium = premium - fee

        policyholder = gl.message.sender_address
        policy_id = self.next_policy_id
        policy = Policy(
            id=policy_id,
            policyholder=policyholder,
            data_source_id=data_source_id,
            flight_code=flight_code,
            flight_date=flight_date,
            flight_date_ts=u256(flight_date_ts),
            premium=u256(premium),
            payout_amount=u256(payout),
            delay_threshold_minutes=delay_threshold_minutes,
            purchased_at=_now_iso(),
            resolvable_at=u256(flight_date_ts + RESOLUTION_BUFFER_SECONDS),
            claim_deadline=u256(flight_date_ts + MAX_CLAIM_WINDOW_SECONDS),
            status=POLICY_ACTIVE,
            decision="",
            observed_status="",
            observed_delay_minutes=u32(0),
            verdict_summary="",
            resolved_at="",
        )
        self.policies[str(policy_id)] = policy
        self._add_active(policy_id)
        self._track_wallet_policy(policyholder, policy_id)
        self.next_policy_id = u256(int(policy_id) + 1)

        self.total_pool_balance = u256(int(self.total_pool_balance) + net_premium)
        self.total_reserved_exposure = u256(int(self.total_reserved_exposure) + payout)
        self.total_premiums_collected = u256(int(self.total_premiums_collected) + premium)

        self._record(
            policy_id,
            "PURCHASED",
            f"{flight_code} on {flight_date}, premium={premium}, payout={payout}",
        )
        self._pay(self.treasury, fee)
        return policy_id

    @gl.public.write
    def resolve_policy(self, policy_id: u256) -> str:
        policy = self._get_policy(policy_id)

        if policy.status != POLICY_ACTIVE:
            raise gl.vm.UserError(f"{ERR_EXPECTED} policy is not awaiting resolution")
        if _now() < int(policy.resolvable_at):
            raise gl.vm.UserError(f"{ERR_EXPECTED} too early -- the flight hasn't happened yet")
        if _now() > int(policy.claim_deadline):
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} claim window has lapsed -- call expire_policy instead"
            )

        source = self._get_data_source(policy.data_source_id)
        if not source.active:
            # The admin deactivated this source -- almost always because it
            # was found to be unreliable or manipulable. Honoring that here
            # too (not just in buy_policy) means disabling a bad source
            # actually protects every policy still pointing at it, instead
            # of only blocking *new* purchases. The policy stays ACTIVE and
            # resolvable again if the source is re-activated; otherwise it
            # falls through to expire_policy once the claim window lapses.
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} data source {policy.data_source_id} has been deactivated; "
                f"wait for it to be re-activated or for the claim window to lapse"
            )
        url_template = source.url_template
        flight_code = policy.flight_code
        flight_date = policy.flight_date
        threshold = int(policy.delay_threshold_minutes)

        def leader_fn():
            def _fetch_status_page(url: str) -> str:
                try:
                    response = gl.nondet.web.get(url)
                except Exception as exc:
                    raise gl.vm.UserError(f"{ERR_LLM} could not fetch the status page: {exc}")
                # An error page (404/5xx) says nothing about the flight. Never
                # feed it to the model: reject so the call can be retried later
                # instead of finalizing a claim on non-evidence.
                if response.status != 200:
                    raise gl.vm.UserError(
                        f"{ERR_LLM} status page returned HTTP {response.status}"
                    )
                text = _clean_page_text((response.body or b"").decode("utf-8", errors="ignore"))
                if not text:
                    raise gl.vm.UserError(f"{ERR_LLM} status page was empty")
                return text

            url = url_template.replace("{FLIGHT}", flight_code).replace("{DATE}", flight_date)
            page_excerpt = _fetch_status_page(url)

            prompt = f"""
You are extracting objective flight status data from a status page. Base
your answer only on the page content given below -- do not guess.

Flight: {flight_code}
Scheduled date (UTC): {flight_date}

Status page content:
{page_excerpt}

Determine that flight's final outcome on that date. Respond with ONLY this
JSON object, no other text:
{{
  "status": "on_time" | "delayed" | "cancelled" | "diverted" | "unknown",
  "delay_minutes": <int, minutes late on arrival; 0 if on_time, cancelled, diverted, or unknown>
}}

Use "unknown" only if the page genuinely does not contain this flight's
status for this date.
"""
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            if not isinstance(raw, dict):
                raise gl.vm.UserError(f"{ERR_LLM} oracle response was not a JSON object")

            status = raw.get("status")
            if status not in VALID_FLIGHT_STATUSES:
                raise gl.vm.UserError(f"{ERR_LLM} invalid status field: {status!r}")

            if status == FLIGHT_STATUS_UNKNOWN:
                # No data (yet) is not a "no" verdict. Rejecting leaves the
                # policy ACTIVE so resolve_policy can be retried once the
                # source has data, or expire_policy applies after the window.
                raise gl.vm.UserError(
                    f"{ERR_LLM} status page has no data for this flight/date yet"
                )

            delay_minutes = _clamp_minutes(raw.get("delay_minutes", 0))
            will_pay = status == FLIGHT_STATUS_CANCELLED or (
                status == FLIGHT_STATUS_DELAYED and delay_minutes >= threshold
            )

            return {
                "decision": DECISION_PAY if will_pay else DECISION_NO_PAY,
                "status": status,
                "delay_minutes": delay_minutes,
            }

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                # Malformed oracle output or an unfetchable page. Never
                # rubber-stamp a failed leader run -- force a rotation.
                return False

            leader_verdict = leaders_res.calldata
            my_verdict = leader_fn()

            if my_verdict["decision"] != leader_verdict["decision"]:
                return False
            if abs(my_verdict["delay_minutes"] - leader_verdict["delay_minutes"]) > DELAY_TOLERANCE_MINUTES:
                # Belt-and-suspenders: agrees on the decision but the
                # underlying reads are wildly different, which smells like
                # a stale or mismatched fetch rather than a genuine
                # coincidence at the threshold boundary.
                return False

            return True

        verdict = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        policy.decision = verdict["decision"]
        policy.observed_status = verdict["status"]
        policy.observed_delay_minutes = u32(verdict["delay_minutes"])
        policy.resolved_at = _now_iso()

        payout = int(policy.payout_amount)
        self.total_reserved_exposure = u256(int(self.total_reserved_exposure) - payout)
        self._remove_active(policy_id)

        if verdict["decision"] == DECISION_PAY:
            policy.status = POLICY_PAID
            policy.verdict_summary = (
                f"{policy.observed_status}, {policy.observed_delay_minutes}m late -- claim paid"
            )
            self.total_pool_balance = u256(int(self.total_pool_balance) - payout)
            self.total_paid_out = u256(int(self.total_paid_out) + payout)
            self.total_policies_paid = u256(int(self.total_policies_paid) + 1)
            self._record(policy_id, "PAID", policy.verdict_summary)
            self._pay(policy.policyholder, payout)
        else:
            policy.status = POLICY_DECLINED
            policy.verdict_summary = (
                f"{policy.observed_status}, {policy.observed_delay_minutes}m late -- below threshold"
            )
            self.total_policies_declined = u256(int(self.total_policies_declined) + 1)
            self._record(policy_id, "DECLINED", policy.verdict_summary)

        return verdict["decision"]

    @gl.public.write
    def expire_policy(self, policy_id: u256) -> None:
        policy = self._get_policy(policy_id)

        if policy.status != POLICY_ACTIVE:
            raise gl.vm.UserError(f"{ERR_EXPECTED} policy is not active")
        if _now() <= int(policy.claim_deadline):
            raise gl.vm.UserError(f"{ERR_EXPECTED} claim window has not lapsed yet")

        payout = int(policy.payout_amount)
        self.total_reserved_exposure = u256(int(self.total_reserved_exposure) - payout)
        self._remove_active(policy_id)

        policy.status = POLICY_EXPIRED
        policy.resolved_at = _now_iso()
        policy.verdict_summary = "claim window lapsed unresolved -- reserved capital released"
        self.total_policies_expired = u256(int(self.total_policies_expired) + 1)

        self._record(policy_id, "EXPIRED", policy.verdict_summary)

    # -----------------------------------------------------------------
    # admin
    # -----------------------------------------------------------------

    @gl.public.write
    def set_protocol_fee_bps(self, new_bps: u32) -> None:
        self._require_admin()
        if new_bps > MAX_PROTOCOL_FEE_BPS:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} protocol_fee_bps cannot exceed {MAX_PROTOCOL_FEE_BPS}"
            )
        self.protocol_fee_bps = new_bps
        self._record(u256(0), "ADMIN_FEE", f"protocol_fee_bps set to {new_bps}")

    @gl.public.write
    def set_treasury(self, new_treasury: str) -> None:
        self._require_admin()
        treasury = Address(new_treasury)
        if treasury == ZERO_ADDRESS:
            # _pay() silently no-ops on the zero address (by design, so a
            # stray zero transfer never reverts a whole settlement). That
            # means a zero-address treasury wouldn't error -- it would just
            # make every future protocol fee vanish permanently, uncounted
            # anywhere. Reject it here instead of discovering that later.
            raise gl.vm.UserError(f"{ERR_EXPECTED} treasury cannot be the zero address")
        self.treasury = treasury
        self._record(u256(0), "ADMIN_TREASURY", f"treasury set to {new_treasury}")

    @gl.public.write
    def set_min_premium_bps(self, new_bps: u32) -> None:
        self._require_admin()
        if new_bps == 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} min_premium_bps must be positive")
        self.min_premium_bps = new_bps
        self._record(u256(0), "ADMIN_MIN_PREMIUM", f"min_premium_bps set to {new_bps}")

    @gl.public.write
    def set_max_payout_multiple(self, new_multiple: u32) -> None:
        self._require_admin()
        if new_multiple == 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} max_payout_multiple must be positive")
        self.max_payout_multiple = new_multiple
        self._record(u256(0), "ADMIN_MAX_MULTIPLE", f"max_payout_multiple set to {new_multiple}")

    @gl.public.write
    def set_max_single_policy_exposure_bps(self, new_bps: u32) -> None:
        self._require_admin()
        if new_bps == 0 or new_bps > MAX_SINGLE_POLICY_EXPOSURE_BPS_CEILING:
            raise gl.vm.UserError(
                f"{ERR_EXPECTED} max_single_policy_exposure_bps must be between 1 and "
                f"{MAX_SINGLE_POLICY_EXPOSURE_BPS_CEILING}"
            )
        self.max_single_policy_exposure_bps = new_bps
        self._record(u256(0), "ADMIN_MAX_EXPOSURE", f"max_single_policy_exposure_bps set to {new_bps}")

    @gl.public.write
    def set_min_deposit(self, new_min_deposit: u256) -> None:
        self._require_admin()
        self.min_deposit = new_min_deposit
        self._record(u256(0), "ADMIN_MIN_DEPOSIT", f"min_deposit set to {new_min_deposit}")

    @gl.public.write
    def set_paused(self, paused: bool) -> None:
        self._require_admin()
        self.paused = paused
        self._record(u256(0), "ADMIN_PAUSE", f"paused set to {paused}")

    @gl.public.write
    def transfer_admin(self, new_admin: str) -> None:
        self._require_admin()
        admin = Address(new_admin)
        if admin == ZERO_ADDRESS:
            # No one can ever sign as the zero address, so this would
            # permanently brick every @gl.public.write admin method --
            # pausing, data sources, fees, treasury, future admin transfers,
            # all of it -- with no recovery path. Reject it outright.
            raise gl.vm.UserError(f"{ERR_EXPECTED} admin cannot be the zero address")
        self.admin = admin
        self._record(u256(0), "ADMIN_TRANSFER", f"admin transferred to {new_admin}")

    # -----------------------------------------------------------------
    # views
    # -----------------------------------------------------------------

    @gl.public.view
    def get_policy(self, policy_id: u256) -> typing.Any:
        return self._get_policy(policy_id)

    @gl.public.view
    def get_data_source(self, data_source_id: u256) -> typing.Any:
        return self._get_data_source(data_source_id)

    @gl.public.view
    def get_data_sources(self, limit: u32) -> list:
        cap = int(limit)
        result = []
        total = int(self.next_data_source_id)
        for i in range(1, total):
            if len(result) >= cap:
                break
            result.append(self.data_sources[str(i)])
        return result

    @gl.public.view
    def get_active_policies(self, limit: u32) -> list:
        cap = int(limit)
        result = []
        for pid in self.active_policy_ids:
            if len(result) >= cap:
                break
            result.append(pid)
        return result

    @gl.public.view
    def get_wallet_policies(self, wallet: str) -> list:
        addr = Address(wallet)
        count = int(self.wallet_policy_count.get(addr, u256(0)))
        result = []
        for i in range(count):
            result.append(self.wallet_policy_index[f"{addr.as_hex}:{i}"])
        return result

    @gl.public.view
    def get_lp_position(self, wallet: str) -> dict[str, str]:
        addr = Address(wallet)
        shares = int(self.pool_shares.get(addr, u256(0)))
        value = 0
        withdrawable = 0
        if int(self.total_shares) > 0:
            value = (shares * int(self.total_pool_balance)) // int(self.total_shares)
            # What withdraw_liquidity would actually pay right now: priced net
            # of capital reserved for active policies.
            withdrawable = (shares * self._available_capacity()) // int(self.total_shares)
        return {"shares": str(shares), "value": str(value), "withdrawable_value": str(withdrawable)}

    @gl.public.view
    def get_audit_log(self, start: u32, count: u32) -> list:
        begin = int(start)
        length = len(self.audit_log)
        end = min(length, begin + int(count))
        result = []
        for i in range(begin, end):
            result.append(self.audit_log[i])
        return result

    @gl.public.view
    def get_stats(self) -> dict[str, str]:
        return {
            "total_policies": str(int(self.next_policy_id) - 1),
            "active_policies": str(len(self.active_policy_ids)),
            "total_policies_paid": str(self.total_policies_paid),
            "total_policies_declined": str(self.total_policies_declined),
            "total_policies_expired": str(self.total_policies_expired),
            "total_pool_balance": str(self.total_pool_balance),
            "total_reserved_exposure": str(self.total_reserved_exposure),
            "available_capacity": str(self._available_capacity()),
            "total_shares": str(self.total_shares),
            "total_premiums_collected": str(self.total_premiums_collected),
            "total_paid_out": str(self.total_paid_out),
            "protocol_fee_bps": str(self.protocol_fee_bps),
            "min_premium_bps": str(self.min_premium_bps),
            "max_payout_multiple": str(self.max_payout_multiple),
            "max_single_policy_exposure_bps": str(self.max_single_policy_exposure_bps),
            "min_deposit": str(self.min_deposit),
            "paused": str(self.paused),
            "admin": self.admin.as_hex,
            "treasury": self.treasury.as_hex,
        }
