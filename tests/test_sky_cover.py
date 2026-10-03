"""
Direct-mode test suite for SkyCover.

Runs the contract's Python code in-process via genlayer-test's Direct Mode
(no Docker, no GenVM, no network).

Install:
    pip install genlayer-test

Run:
    pytest tests/ -v
"""

import datetime as dt
import json
from pathlib import Path

import pytest
from gltest.direct.loader import create_address, deploy_contract
from gltest.direct.sdk_loader import setup_sdk_paths
from gltest.direct.vm import VMContext

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contracts" / "sky_cover.py"

# Pinned to the GenVM release whose runner hash matches the "Depends" header
# in sky_cover.py. Direct Mode downloads and caches this release once
# (~200MB) under ~/.cache/gltest-direct/ the first time the suite runs.
SDK_VERSION = "v0.2.12"

PROTOCOL_FEE_BPS = 500          # 5% of every premium
MIN_PREMIUM_BPS = 500           # premium must be worth >= 5% of the payout
MAX_PAYOUT_MULTIPLE = 10        # payout <= 10x the premium
MAX_SINGLE_POLICY_EXPOSURE_BPS = 2000  # a policy can't reserve more than 20% of the pool
MIN_DEPOSIT = 100

ZERO_ADDRESS_HEX = "0x0000000000000000000000000000000000000000"
STATUS_URL_TEMPLATE = "https://status.example.com/track/{FLIGHT}/{DATE}"
STATUS_URL_PATTERN = r"status\.example\.com"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def world():
    """Deploy exactly one SkyCover instance for the whole test session.

    genlayer-test's Direct Mode registers a contract's Python class the
    first time its module is executed and refuses to register a second
    Contract subclass in the same process, so redeploying per test isn't
    an option here -- deploy once and isolate each test with
    vm.snapshot()/vm.revert() instead (see `isolated` below).
    """
    vm = VMContext()
    with vm.activate():
        setup_sdk_paths(CONTRACT_PATH, SDK_VERSION)

        admin = create_address("sky_admin")
        alice = create_address("sky_alice")  # a liquidity provider
        bob = create_address("sky_bob")      # a liquidity provider
        carol = create_address("sky_carol")  # a traveler
        dave = create_address("sky_dave")    # a traveler / third party

        vm.sender = admin
        contract = deploy_contract(
            str(CONTRACT_PATH),
            vm,
            PROTOCOL_FEE_BPS,
            MIN_PREMIUM_BPS,
            MAX_PAYOUT_MULTIPLE,
            MAX_SINGLE_POLICY_EXPOSURE_BPS,
            MIN_DEPOSIT,
            sdk_version=SDK_VERSION,
        )

        # One shared, active data source every test can buy a policy against.
        vm.sender = admin
        source_id = contract.add_data_source("StatusExample", STATUS_URL_TEMPLATE)

        yield {
            "vm": vm,
            "contract": contract,
            "admin": admin,
            "alice": alice,
            "bob": bob,
            "carol": carol,
            "dave": dave,
            "source_id": source_id,
        }


@pytest.fixture
def isolated(world):
    """Snapshot before a test and revert after it, so tests don't leak
    liquidity, policies, or admin changes into one another despite
    sharing the one deployed instance."""
    vm = world["vm"]
    snap = vm.snapshot()
    try:
        yield world
    finally:
        vm.revert(snap)


def _date_str(days_from_now: int) -> str:
    target = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days_from_now)
    return target.strftime("%Y-%m-%d")


def _warp_to(world, timestamp: int, offset_seconds: int = 0):
    target = dt.datetime.fromtimestamp(int(timestamp) + offset_seconds, tz=dt.timezone.utc)
    world["vm"].warp(target.isoformat())


def _deposit(world, amount, lp=None):
    vm, contract = world["vm"], world["contract"]
    vm.sender = lp or world["alice"]
    vm.value = amount
    shares = contract.deposit_liquidity()
    vm.value = 0
    return shares


def _buy_policy(
    world,
    premium=1_000,
    payout=5_000,
    flight_code="AA123",
    days_out=5,
    delay_threshold=120,
    traveler=None,
    source_id=None,
):
    vm, contract = world["vm"], world["contract"]
    vm.sender = traveler or world["carol"]
    vm.value = premium
    policy_id = contract.buy_policy(
        source_id if source_id is not None else world["source_id"],
        flight_code,
        _date_str(days_out),
        delay_threshold,
        payout,
    )
    vm.value = 0
    return policy_id


def _mock_flight(world, status="cancelled", delay_minutes=0):
    world["vm"].mock_web(STATUS_URL_PATTERN, {"status": 200, "body": f"flight status: {status}"})
    world["vm"].mock_llm(
        r"Determine that flight's final outcome",
        json.dumps({"status": status, "delay_minutes": delay_minutes}),
    )


# ---------------------------------------------------------------------------
# constructor / initial state
# ---------------------------------------------------------------------------


def test_initial_state(isolated):
    stats = isolated["contract"].get_stats()
    assert stats["total_policies"] == "0"
    assert stats["active_policies"] == "0"
    assert stats["total_pool_balance"] == "0"
    assert stats["total_reserved_exposure"] == "0"
    assert stats["protocol_fee_bps"] == str(PROTOCOL_FEE_BPS)
    assert stats["paused"] == "False"
    assert stats["admin"] == isolated["admin"].as_hex


# Note: the constructor's own bound checks (protocol_fee_bps <= 2000,
# 0 < max_single_policy_exposure_bps <= 5000, min_premium_bps > 0,
# max_payout_multiple > 0) aren't exercised by deploying a second,
# deliberately-invalid instance -- Direct Mode only allows one Contract
# class registration per process (see the `world` fixture docstring).
# They're covered instead via the matching admin setters further down
# (test_protocol_fee_is_capped, test_max_single_policy_exposure_bps_is_bounded,
# test_min_premium_bps_and_max_payout_multiple_must_be_positive), which run
# the exact same checks against the exact same constants.


# ---------------------------------------------------------------------------
# liquidity pool
# ---------------------------------------------------------------------------


def test_first_deposit_mints_shares_one_to_one(isolated):
    shares = _deposit(isolated, 10_000)
    assert int(shares) == 10_000

    position = isolated["contract"].get_lp_position(isolated["alice"].as_hex)
    assert position["shares"] == "10000"
    assert position["value"] == "10000"


def test_second_deposit_is_proportional_after_the_pool_grows(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000, lp=isolated["alice"])

    # Grow the pool via a policy premium so the share price is no longer 1:1.
    _buy_policy(isolated, premium=1_000, payout=5_000)
    stats = contract.get_stats()
    pool_before = int(stats["total_pool_balance"])
    assert pool_before > 100_000  # net premium landed in the pool

    bob_shares = _deposit(isolated, 5_000, lp=isolated["bob"])
    expected = (5_000 * 100_000) // pool_before
    assert int(bob_shares) == expected


def test_withdraw_liquidity_pays_out_proportional_value(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 10_000)

    isolated["vm"].sender = isolated["alice"]
    payout = contract.withdraw_liquidity(4_000)
    assert int(payout) == 4_000

    position = contract.get_lp_position(isolated["alice"].as_hex)
    assert position["shares"] == "6000"


def test_cannot_withdraw_more_shares_than_owned(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 10_000)
    isolated["vm"].sender = isolated["alice"]
    with pytest.raises(Exception, match="only owns"):
        contract.withdraw_liquidity(10_001)


def test_withdraw_blocked_by_capital_reserved_for_active_policies(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 10_000)
    _buy_policy(isolated, premium=1_000, payout=1_500)  # reserves 1,500

    isolated["vm"].sender = isolated["alice"]
    # Withdrawing every share while a policy still reserves capital would
    # orphan that capital, so the last shares are blocked outright.
    with pytest.raises(Exception, match="last shares cannot be withdrawn"):
        contract.withdraw_liquidity(10_000)

    # A partial exit is allowed but priced net of the reserved 1,500.
    payout = contract.withdraw_liquidity(5_000)
    pool_balance = 10_000 + (1_000 - (1_000 * PROTOCOL_FEE_BPS) // 10_000)
    assert int(payout) == (5_000 * (pool_balance - 1_500)) // 10_000


def test_deposit_below_minimum_is_rejected(isolated):
    with pytest.raises(Exception, match="below the minimum"):
        _deposit(isolated, MIN_DEPOSIT - 1)


# ---------------------------------------------------------------------------
# data source registry
# ---------------------------------------------------------------------------


def test_only_admin_can_add_a_data_source(isolated):
    isolated["vm"].sender = isolated["alice"]
    with pytest.raises(Exception, match="not the admin"):
        isolated["contract"].add_data_source("Rogue", "https://evil.example.com/{FLIGHT}/{DATE}")


def test_data_source_template_must_have_both_placeholders(isolated):
    isolated["vm"].sender = isolated["admin"]
    with pytest.raises(Exception, match="placeholders"):
        isolated["contract"].add_data_source("Bad", "https://example.com/{FLIGHT}")


def test_data_source_template_must_be_http(isolated):
    isolated["vm"].sender = isolated["admin"]
    with pytest.raises(Exception, match="http"):
        isolated["contract"].add_data_source("Bad", "ftp://example.com/{FLIGHT}/{DATE}")


def test_deactivated_data_source_cannot_be_used_for_new_policies(isolated):
    vm, contract = isolated["vm"], isolated["contract"]
    vm.sender = isolated["admin"]
    contract.set_data_source_active(isolated["source_id"], False)

    with pytest.raises(Exception, match="no longer active"):
        _buy_policy(isolated)


# ---------------------------------------------------------------------------
# buying a policy: validation
# ---------------------------------------------------------------------------


def test_buy_policy_rejects_invalid_flight_code(isolated):
    with pytest.raises(Exception, match="flight_code"):
        _buy_policy(isolated, flight_code="a")


def test_buy_policy_rejects_malformed_flight_date(isolated):
    vm, contract = isolated["vm"], isolated["contract"]
    vm.sender = isolated["carol"]
    vm.value = 1_000
    with pytest.raises(Exception, match="YYYY-MM-DD"):
        contract.buy_policy(isolated["source_id"], "AA123", "05/01/2030", 120, 5_000)
    vm.value = 0


def test_buy_policy_rejects_flight_too_soon(isolated):
    with pytest.raises(Exception, match="at least"):
        _buy_policy(isolated, days_out=0)


def test_buy_policy_rejects_flight_too_far_out(isolated):
    with pytest.raises(Exception, match="days out"):
        _buy_policy(isolated, days_out=200)


def test_buy_policy_rejects_delay_threshold_out_of_bounds(isolated):
    with pytest.raises(Exception, match="delay_threshold_minutes"):
        _buy_policy(isolated, delay_threshold=10)


def test_buy_policy_rejects_zero_premium(isolated):
    with pytest.raises(Exception, match="premium must be greater than zero"):
        _buy_policy(isolated, premium=0, payout=5_000)


def test_buy_policy_rejects_zero_payout(isolated):
    with pytest.raises(Exception, match="payout_amount must be greater than zero"):
        _buy_policy(isolated, premium=1_000, payout=0)


def test_buy_policy_rejects_payout_beyond_max_multiple(isolated):
    with pytest.raises(Exception, match="max_payout_multiple|cannot exceed"):
        _buy_policy(isolated, premium=100, payout=100 * MAX_PAYOUT_MULTIPLE + 1)


def test_buy_policy_rejects_premium_below_min_bps(isolated):
    # max_payout_multiple (10x) implies a stricter 10% floor than
    # min_premium_bps (5%) at the deployed defaults, so relax the multiple
    # first to isolate the bps check specifically.
    isolated["vm"].sender = isolated["admin"]
    isolated["contract"].set_max_payout_multiple(1000)
    # payout=10,000 needs premium >= 5% = 500
    with pytest.raises(Exception, match="premium is too low"):
        _buy_policy(isolated, premium=100, payout=10_000)


def test_buy_policy_rejects_when_pool_capacity_is_insufficient(isolated):
    _deposit(isolated, 1_000)
    with pytest.raises(Exception, match="uncommitted capital"):
        _buy_policy(isolated, premium=1_000, payout=5_000)


def test_buy_policy_rejects_beyond_concentration_limit(isolated):
    _deposit(isolated, 100_000)
    # 20% of 100,000 == 20,000; ask for more than that.
    with pytest.raises(Exception, match="concentration limit"):
        _buy_policy(isolated, premium=5_000, payout=25_000)


def test_paused_pool_blocks_new_policies_and_deposits(isolated):
    vm, contract = isolated["vm"], isolated["contract"]
    vm.sender = isolated["admin"]
    contract.set_paused(True)
    try:
        with pytest.raises(Exception, match="paused"):
            _deposit(isolated, 10_000)
        with pytest.raises(Exception, match="paused"):
            _buy_policy(isolated)
    finally:
        vm.sender = isolated["admin"]
        contract.set_paused(False)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def test_full_lifecycle_cancelled_flight_pays_out(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, premium=1_000, payout=5_000, delay_threshold=120)

    policy = contract.get_policy(policy_id)
    assert policy.status == "ACTIVE"

    with pytest.raises(Exception, match="hasn't happened yet"):
        contract.resolve_policy(policy_id)

    _mock_flight(isolated, status="cancelled")
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)

    decision = contract.resolve_policy(policy_id)
    assert decision == "pay"

    policy = contract.get_policy(policy_id)
    assert policy.status == "PAID"
    assert policy.observed_status == "cancelled"

    stats = contract.get_stats()
    assert stats["total_policies_paid"] == "1"
    assert stats["active_policies"] == "0"
    assert stats["total_reserved_exposure"] == "0"

    log = contract.get_audit_log(0, 20)
    actions = [entry.action for entry in log]
    assert "PURCHASED" in actions and "PAID" in actions


def test_on_time_flight_declines_the_claim(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, delay_threshold=120)
    policy = contract.get_policy(policy_id)

    _mock_flight(isolated, status="on_time", delay_minutes=0)
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)

    decision = contract.resolve_policy(policy_id)
    assert decision == "no_pay"
    assert contract.get_policy(policy_id).status == "DECLINED"
    assert contract.get_stats()["total_policies_declined"] == "1"


def test_delay_below_threshold_declines_the_claim(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, delay_threshold=120)
    policy = contract.get_policy(policy_id)

    _mock_flight(isolated, status="delayed", delay_minutes=90)
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)

    assert contract.resolve_policy(policy_id) == "no_pay"


def test_delay_at_or_beyond_threshold_pays_out(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, delay_threshold=120)
    policy = contract.get_policy(policy_id)

    _mock_flight(isolated, status="delayed", delay_minutes=120)
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)

    assert contract.resolve_policy(policy_id) == "pay"


def test_cannot_resolve_after_the_claim_deadline(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated)
    policy = contract.get_policy(policy_id)

    _mock_flight(isolated, status="cancelled")
    _warp_to(isolated, policy.claim_deadline, offset_seconds=1)

    with pytest.raises(Exception, match="expire_policy instead"):
        contract.resolve_policy(policy_id)


def test_cannot_resolve_a_policy_twice(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated)
    policy = contract.get_policy(policy_id)

    _mock_flight(isolated, status="cancelled")
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)
    contract.resolve_policy(policy_id)

    with pytest.raises(Exception, match="not awaiting resolution"):
        contract.resolve_policy(policy_id)


# ---------------------------------------------------------------------------
# expiry
# ---------------------------------------------------------------------------


def test_expire_before_deadline_is_rejected(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated)
    policy = contract.get_policy(policy_id)

    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)
    with pytest.raises(Exception, match="has not lapsed"):
        contract.expire_policy(policy_id)


def test_expire_after_deadline_releases_capital_with_no_payout(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, premium=1_000, payout=5_000)
    policy = contract.get_policy(policy_id)

    _warp_to(isolated, policy.claim_deadline, offset_seconds=1)
    contract.expire_policy(policy_id)

    policy = contract.get_policy(policy_id)
    assert policy.status == "EXPIRED"

    stats = contract.get_stats()
    assert stats["total_reserved_exposure"] == "0"
    assert stats["total_policies_expired"] == "1"
    assert stats["active_policies"] == "0"


def test_expired_policy_cannot_be_expired_again(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated)
    policy = contract.get_policy(policy_id)

    _warp_to(isolated, policy.claim_deadline, offset_seconds=1)
    contract.expire_policy(policy_id)
    with pytest.raises(Exception, match="not active"):
        contract.expire_policy(policy_id)


# ---------------------------------------------------------------------------
# the equivalence-principle judging pattern itself
# ---------------------------------------------------------------------------


def test_validator_agrees_within_tolerance(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, delay_threshold=120)
    policy = contract.get_policy(policy_id)

    _mock_flight(isolated, status="delayed", delay_minutes=130)
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)
    contract.resolve_policy(policy_id)

    assert isolated["vm"].run_validator() is True


def test_validator_disagrees_on_a_different_decision(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, delay_threshold=120)
    policy = contract.get_policy(policy_id)

    _mock_flight(isolated, status="delayed", delay_minutes=130)
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)
    contract.resolve_policy(policy_id)

    isolated["vm"].clear_mocks()
    _mock_flight(isolated, status="on_time", delay_minutes=0)
    assert isolated["vm"].run_validator() is False


def test_validator_disagrees_when_minutes_drift_too_far_despite_same_decision(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, delay_threshold=60)
    policy = contract.get_policy(policy_id)

    _mock_flight(isolated, status="delayed", delay_minutes=70)
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)
    contract.resolve_policy(policy_id)

    # Both reads agree on "pay" (70 >= 60 and 400 >= 60), but the
    # underlying minutes are wildly different -- the belt-and-suspenders
    # check should still catch this as a bad read rather than a
    # coincidence at the threshold boundary.
    isolated["vm"].clear_mocks()
    _mock_flight(isolated, status="delayed", delay_minutes=400)
    assert isolated["vm"].run_validator() is False


def test_malformed_oracle_response_is_rejected(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated)
    policy = contract.get_policy(policy_id)

    isolated["vm"].mock_web(STATUS_URL_PATTERN, {"status": 200, "body": "flight status: unclear"})
    isolated["vm"].mock_llm(r"Determine that flight's final outcome", json.dumps({"status": "banana"}))
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)

    with pytest.raises(Exception):
        contract.resolve_policy(policy_id)


def test_unfetchable_status_page_is_rejected(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated)
    policy = contract.get_policy(policy_id)

    # Deliberately no vm.mock_web() registered for this URL -- the fetch
    # itself should fail and the resolution should be rejected outright
    # rather than silently defaulting to a decision either way.
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)
    with pytest.raises(Exception):
        contract.resolve_policy(policy_id)


# ---------------------------------------------------------------------------
# admin
# ---------------------------------------------------------------------------


def test_only_admin_can_change_protocol_settings(isolated):
    contract = isolated["contract"]
    isolated["vm"].sender = isolated["alice"]
    with pytest.raises(Exception, match="not the admin"):
        contract.set_protocol_fee_bps(1000)


def test_protocol_fee_is_capped(isolated):
    isolated["vm"].sender = isolated["admin"]
    with pytest.raises(Exception, match="cannot exceed"):
        isolated["contract"].set_protocol_fee_bps(2001)


def test_max_single_policy_exposure_bps_is_bounded(isolated):
    vm, contract = isolated["vm"], isolated["contract"]
    vm.sender = isolated["admin"]
    with pytest.raises(Exception, match="between 1 and"):
        contract.set_max_single_policy_exposure_bps(5001)
    with pytest.raises(Exception, match="between 1 and"):
        contract.set_max_single_policy_exposure_bps(0)


def test_min_premium_bps_and_max_payout_multiple_must_be_positive(isolated):
    vm, contract = isolated["vm"], isolated["contract"]
    vm.sender = isolated["admin"]
    with pytest.raises(Exception, match="must be positive"):
        contract.set_min_premium_bps(0)
    with pytest.raises(Exception, match="must be positive"):
        contract.set_max_payout_multiple(0)


def test_admin_can_transfer_admin_role(isolated):
    vm, contract = isolated["vm"], isolated["contract"]
    vm.sender = isolated["admin"]
    contract.transfer_admin(isolated["dave"].as_hex)

    vm.sender = isolated["dave"]
    contract.set_paused(True)  # only works if dave is now really the admin
    assert contract.get_stats()["paused"] == "True"
    contract.set_paused(False)


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------


def test_get_wallet_policies(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    pid1 = _buy_policy(isolated, traveler=isolated["carol"], flight_code="AA111")
    pid2 = _buy_policy(isolated, traveler=isolated["carol"], flight_code="BB222")
    _buy_policy(isolated, traveler=isolated["dave"], flight_code="CC333")

    carol_ids = [int(x) for x in contract.get_wallet_policies(isolated["carol"].as_hex)]
    assert carol_ids == [int(pid1), int(pid2)]


def test_get_wallet_policies_keeps_full_history_across_every_status(isolated):
    # Regression test for the wallet-index fix: get_wallet_policies must
    # keep returning a policy after it moves out of ACTIVE (PAID, DECLINED,
    # or EXPIRED), since it's keyed by an append-only per-wallet counter,
    # not by scanning whatever is currently active.
    contract = isolated["contract"]
    _deposit(isolated, 100_000)

    paid_id = _buy_policy(isolated, flight_code="AA111", delay_threshold=60)
    declined_id = _buy_policy(isolated, flight_code="BB222", delay_threshold=60)
    expired_id = _buy_policy(isolated, flight_code="CC333")

    policy = contract.get_policy(paid_id)
    _mock_flight(isolated, status="cancelled")
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)
    contract.resolve_policy(paid_id)

    policy = contract.get_policy(declined_id)
    isolated["vm"].clear_mocks()
    _mock_flight(isolated, status="on_time")
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)
    contract.resolve_policy(declined_id)

    policy = contract.get_policy(expired_id)
    _warp_to(isolated, policy.claim_deadline, offset_seconds=1)
    contract.expire_policy(expired_id)

    ids = [int(x) for x in contract.get_wallet_policies(isolated["carol"].as_hex)]
    assert ids == [int(paid_id), int(declined_id), int(expired_id)]
    statuses = [contract.get_policy(i).status for i in ids]
    assert statuses == ["PAID", "DECLINED", "EXPIRED"]


def test_get_active_policies_shrinks_after_resolution(isolated):
    contract = isolated["contract"]
    _deposit(isolated, 100_000)
    pid1 = _buy_policy(isolated, flight_code="AA111")
    _buy_policy(isolated, flight_code="BB222")

    assert len(contract.get_active_policies(50)) == 2

    policy = contract.get_policy(pid1)
    _mock_flight(isolated, status="cancelled")
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)
    contract.resolve_policy(pid1)

    remaining = [int(x) for x in contract.get_active_policies(50)]
    assert int(pid1) not in remaining
    assert len(remaining) == 1


def test_active_policy_index_stays_correct_through_a_middle_removal(isolated):
    # Regression test for the O(1) swap-and-pop fix: removing a policy
    # from the *middle* of active_policy_ids moves the last element into
    # its slot, and active_policy_index must be updated for that moved
    # element too, or a later removal would look up a stale/wrong index.
    contract = isolated["contract"]
    _deposit(isolated, 1_000_000)

    ids = [
        _buy_policy(isolated, flight_code=f"AA{i:03d}", payout=1_000)
        for i in range(5)
    ]
    assert [int(x) for x in contract.get_active_policies(50)] == [int(i) for i in ids]

    # Remove the middle one (index 2 of 5) via expire_policy -- this moves
    # the last id (index 4) into slot 2.
    middle = ids[2]
    policy = contract.get_policy(middle)
    _warp_to(isolated, policy.claim_deadline, offset_seconds=1)
    contract.expire_policy(middle)

    remaining_after_first = [int(x) for x in contract.get_active_policies(50)]
    assert int(middle) not in remaining_after_first
    assert len(remaining_after_first) == 4
    # The element that was moved into the vacated slot (originally last)
    # must still be independently removable afterwards.
    moved = ids[4]
    policy = contract.get_policy(moved)
    _warp_to(isolated, policy.claim_deadline, offset_seconds=1)
    contract.expire_policy(moved)

    remaining_after_second = [int(x) for x in contract.get_active_policies(50)]
    assert int(moved) not in remaining_after_second
    assert len(remaining_after_second) == 3
    # And every survivor must still be resolvable/expirable on its own --
    # if index tracking were broken, one of these would now point at the
    # wrong array slot.
    for pid in remaining_after_second:
        policy = contract.get_policy(pid)
        _warp_to(isolated, policy.claim_deadline, offset_seconds=1)
        contract.expire_policy(pid)
    assert contract.get_active_policies(50) == []


def test_nonexistent_policy_and_data_source_are_reported_clearly(isolated):
    contract = isolated["contract"]
    with pytest.raises(Exception, match="does not exist"):
        contract.get_policy(999_999)
    with pytest.raises(Exception, match="does not exist"):
        contract.get_data_source(999_999)


def test_audit_log_pagination(isolated):
    contract = isolated["contract"]
    baseline = len(contract.get_audit_log(0, 1000))

    _deposit(isolated, 100_000)
    _buy_policy(isolated)

    first_page = contract.get_audit_log(baseline, 1)
    assert len(first_page) == 1
    assert first_page[0].action == "LP_DEPOSIT"

    second_page = contract.get_audit_log(baseline + 1, 1)
    assert len(second_page) == 1
    assert second_page[0].action == "PURCHASED"


# ---------------------------------------------------------------------------
# regression tests for the bug-review fixes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("http_status", [404, 503])
def test_http_error_page_never_finalizes_a_claim(isolated, http_status):
    contract, vm = isolated["contract"], isolated["vm"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, delay_threshold=120)
    policy = contract.get_policy(policy_id)

    # Source is down. Even if the model were (wrongly) willing to answer, the
    # error page must be rejected before it ever reaches the prompt.
    vm.mock_web(STATUS_URL_PATTERN, {"status": http_status, "body": "Service Unavailable"})
    vm.mock_llm(
        r"Determine that flight's final outcome",
        json.dumps({"status": "on_time", "delay_minutes": 0}),
    )
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)

    with pytest.raises(Exception, match=f"HTTP {http_status}"):
        contract.resolve_policy(policy_id)

    # Nothing was finalized: still ACTIVE, capital still reserved, retryable.
    assert contract.get_policy(policy_id).status == "ACTIVE"
    assert contract.get_stats()["active_policies"] == "1"


def test_unknown_status_leaves_policy_active_and_retryable(isolated):
    contract, vm = isolated["contract"], isolated["vm"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, premium=1_000, payout=5_000, delay_threshold=120)
    policy = contract.get_policy(policy_id)
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)

    # The page is up but doesn't have the flight yet.
    _mock_flight(isolated, status="unknown")
    with pytest.raises(Exception, match="no data for this flight"):
        contract.resolve_policy(policy_id)
    assert contract.get_policy(policy_id).status == "ACTIVE"
    assert contract.get_stats()["total_reserved_exposure"] == "5000"

    # Later the source has real data; the same policy can now be resolved.
    vm.clear_mocks()
    _mock_flight(isolated, status="cancelled")
    assert contract.resolve_policy(policy_id) == "pay"
    assert contract.get_policy(policy_id).status == "PAID"


def test_status_text_is_found_even_behind_a_huge_script_block(isolated):
    contract, vm = isolated["contract"], isolated["vm"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, delay_threshold=120)
    policy = contract.get_policy(policy_id)

    # >20 KB of <head> noise before the real content: previously the first
    # 4000 raw characters were all the model ever saw.
    body = (
        "<html><head><style>" + "a{b:c}" * 3000 + "</style>"
        "<script>" + "var x=1;" * 3000 + "</script></head>"
        "<body><p>Final status: CANCELLED_BY_AIRLINE</p></body></html>"
    )
    vm.mock_web(STATUS_URL_PATTERN, {"status": 200, "body": body})
    # Only answers if the visible page text actually reached the prompt, and
    # not if raw markup did.
    vm.mock_llm(
        r"(?s)^(?!.*<script).*CANCELLED_BY_AIRLINE",
        json.dumps({"status": "cancelled", "delay_minutes": 0}),
    )
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)

    assert contract.resolve_policy(policy_id) == "pay"


def test_flight_code_and_date_reject_newlines_and_non_ascii(isolated):
    contract, vm = isolated["contract"], isolated["vm"]
    _deposit(isolated, 100_000)
    vm.sender = isolated["carol"]
    vm.value = 1_000
    good_date = _date_str(5)

    with pytest.raises(Exception, match="flight_code must be 2-10"):
        contract.buy_policy(isolated["source_id"], "AA123\n", good_date, 120, 5_000)
    with pytest.raises(Exception, match="must be ASCII"):
        contract.buy_policy(isolated["source_id"], "aa\u013123", good_date, 120, 5_000)
    with pytest.raises(Exception, match="YYYY-MM-DD"):
        contract.buy_policy(isolated["source_id"], "AA123", good_date + "\n", 120, 5_000)
    arabic_indic = good_date.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
    with pytest.raises(Exception, match="YYYY-MM-DD"):
        contract.buy_policy(isolated["source_id"], "AA123", arabic_indic, 120, 5_000)
    vm.value = 0

    assert contract.get_stats()["total_policies"] == "0"


def test_informed_lp_cannot_exit_ahead_of_a_known_payout(isolated):
    contract, vm = isolated["contract"], isolated["vm"]
    alice, bob = isolated["alice"], isolated["bob"]
    _deposit(isolated, 50_000, lp=alice)
    bob_shares = _deposit(isolated, 50_000, lp=bob)

    premium, payout = 1_000, 10_000
    policy_id = _buy_policy(isolated, premium=premium, payout=payout)
    policy = contract.get_policy(policy_id)
    net_premium = premium - (premium * PROTOCOL_FEE_BPS) // 10_000
    pool = 100_000 + net_premium
    fair_value_each = (pool - payout) // 2  # what each LP ends with if the claim pays

    # The flight is already cancelled and Bob knows it -- he tries to leave first.
    vm.sender = bob
    bob_got = int(contract.withdraw_liquidity(bob_shares))
    assert bob_got == fair_value_each  # priced net of the pending 10,000 liability
    # The UI figure matches what a withdrawal would really pay.
    assert contract.get_lp_position(alice.as_hex)["withdrawable_value"] == str(fair_value_each)

    _mock_flight(isolated, status="cancelled")
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)
    vm.sender = isolated["dave"]
    assert contract.resolve_policy(policy_id) == "pay"

    # Alice ends up exactly where she would have if Bob had stayed in.
    assert contract.get_lp_position(alice.as_hex)["value"] == str(fair_value_each)


# ---------------------------------------------------------------------------
# tests for the patched behaviours (zero-balance guards, source deactivation,
# zero-address guards)
# ---------------------------------------------------------------------------

ZERO_ADDR_HEX = "0x" + "0" * 40


def test_fully_reserved_pool_never_burns_shares_for_zero(isolated):
    """balance == reserved is a normal state, NOT a wipe-out: a withdrawal
    that rounds to 0 must revert instead of destroying the LP's shares."""
    contract, vm, alice = isolated["contract"], isolated["vm"], isolated["alice"]
    _deposit(isolated, 1_000, lp=alice)
    _deposit(isolated, 1_000, lp=isolated["bob"])
    # Force "all capital reserved" directly in storage (the policy purchase
    # path can't reach it exactly, but the guard must hold regardless).
    contract.total_reserved_exposure = int(contract.total_pool_balance)

    vm.sender = alice
    shares_before = contract.get_lp_position(alice.as_hex)["shares"]
    with pytest.raises(Exception, match="nothing to withdraw"):
        contract.withdraw_liquidity(500)
    assert contract.get_lp_position(alice.as_hex)["shares"] == shares_before


def test_wiped_out_pool_blocks_deposits_but_lets_lps_burn_dead_shares(isolated):
    contract, vm = isolated["contract"], isolated["vm"]
    alice, bob = isolated["alice"], isolated["bob"]
    _deposit(isolated, 1_000, lp=alice)
    # Simulate claims having drained the whole pool while shares remain.
    contract.total_pool_balance = 0
    contract.total_reserved_exposure = 0

    # New money must not be minted against dead shares (or divide by zero).
    with pytest.raises(Exception, match="fully drained"):
        _deposit(isolated, 500, lp=bob)

    # The old LP can formally burn the worthless shares for 0 GEN...
    vm.sender = alice
    assert int(contract.withdraw_liquidity(1_000)) == 0
    assert contract.get_stats()["total_shares"] == "0"

    # ...after which the pool bootstraps 1:1 again.
    assert int(_deposit(isolated, 500, lp=bob)) == 500


def test_resolve_is_blocked_while_the_source_is_deactivated_and_resumes_after(isolated):
    contract, vm = isolated["contract"], isolated["vm"]
    _deposit(isolated, 100_000)
    policy_id = _buy_policy(isolated, premium=1_000, payout=5_000, delay_threshold=120)
    policy = contract.get_policy(policy_id)
    _mock_flight(isolated, status="cancelled")
    _warp_to(isolated, policy.resolvable_at, offset_seconds=1)

    vm.sender = isolated["admin"]
    contract.set_data_source_active(isolated["source_id"], False)
    vm.sender = isolated["dave"]
    with pytest.raises(Exception, match="has been deactivated"):
        contract.resolve_policy(policy_id)
    assert contract.get_policy(policy_id).status == "ACTIVE"

    vm.sender = isolated["admin"]
    contract.set_data_source_active(isolated["source_id"], True)
    vm.sender = isolated["dave"]
    assert contract.resolve_policy(policy_id) == "pay"


def test_treasury_and_admin_cannot_be_set_to_the_zero_address(isolated):
    contract, vm = isolated["contract"], isolated["vm"]
    vm.sender = isolated["admin"]
    with pytest.raises(Exception, match="treasury cannot be the zero address"):
        contract.set_treasury(ZERO_ADDR_HEX)
    with pytest.raises(Exception, match="admin cannot be the zero address"):
        contract.transfer_admin(ZERO_ADDR_HEX)
    stats = contract.get_stats()
    assert stats["admin"] == isolated["admin"].as_hex
