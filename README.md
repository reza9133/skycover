# SkyCover

**Parametric flight-delay insurance, underwritten by a GenLayer-judged liquidity pool.**

> Status: the contract and its test suite are complete and verified.
> Frontend and deployment scripts are not built yet — this repo currently
> holds `contracts/` and `tests/` only, by design, while the contract
> itself gets nailed down first.

Liquidity providers deposit GEN into a shared pool and receive shares.
Travelers buy a policy against a specific flight — airline/flight code
plus a scheduled UTC date — naming a payout amount and a delay threshold
in minutes, and pay a premium that's added to the pool. Once the flight's
date has passed, **anyone** can call `resolve_policy`: GenLayer validators
independently fetch the flight's status from an admin-curated data
source, extract a structured outcome, and must agree on the same pay /
no-pay decision before a single unit of GEN moves. A cancelled flight, or
a delay at or beyond the policy's threshold, pays the policyholder out of
the pool; anything else releases the reserved capital back to the pool
with no payout.

## Why this needs GenLayer

This is close to a headline example in GenLayer's own use-case catalogue:

> "Insurance — parametric and evidence-based claims evaluated by AI
> validators. Contracts fetch weather data, flight statuses, or
> photographic evidence to assess claims and trigger payouts
> automatically — no adjusters, no weeks of waiting."
> — [docs.genlayer.com](https://docs.genlayer.com/understand-genlayer-protocol/typical-use-cases)

A flight-status page is unstructured HTML written for humans, not an
oracle feed. Turning "Flight AA123 — Cancelled" or "Landed 2h 14m late"
into a structured, on-chain fact that a diverse set of independent
validators actually agrees on is exactly the "judgment call" a plain
deterministic contract — or a single centralized oracle — can't do on its
own.

## How it works

```
LPs ──deposit──► POOL ◄──premium── buy_policy() ──► Policy [ACTIVE]
      ▲ shares                        (reserves payout_amount
      │                                from the pool's capacity)
      │
      └── withdraw_liquidity()                  scheduled flight date
          (blocked from touching                        │
           capital reserved for                         ▼
           active policies)                  resolvable_at (flight date
                                              + 30h buffer) has passed
                                                          │
                                    ┌─────────────────────┴─────────────────────┐
                                    ▼                                           ▼
                         resolve_policy()                          expire_policy()
                    (GenLayer fetches status,                 (nobody resolved within
                     validators agree on                       the 30-day claim window;
                     pay / no_pay)                              releases the reserve,
                                    │                            no payout either way)
                     ┌──────────────┴──────────────┐
                     ▼                              ▼
                  PAID                          DECLINED
          (payout_amount sent           (premium stays in the
           from the pool)                pool as underwriting
                                          profit for LPs)
```

## The verdict

Travelers never supply the URL that gets fetched. At purchase time they
pick from a small set of **data sources the admin has registered** — a
name plus a URL template containing `{FLIGHT}` and `{DATE}` placeholders
(e.g. `https://status.example.com/track/{FLIGHT}/{DATE}`) — and the
contract fills in the template itself at resolution time. This closes off
the obvious attack of a policyholder pointing the oracle at a server they
control that always says "cancelled."

`resolve_policy` fetches that URL and asks the model for exactly this
shape:

```json
{
  "status": "on_time" | "delayed" | "cancelled" | "diverted" | "unknown",
  "delay_minutes": 0
}
```

The leader derives a single decision field from that — `"pay"` if the
flight was cancelled, or delayed at or beyond the policy's own threshold;
`"no_pay"` otherwise. Every validator re-fetches the same URL and re-runs
the same prompt independently, and only agrees with the leader if:

1. its own derived **`decision` matches exactly**, and
2. `delay_minutes` is within **±15 minutes** as a secondary sanity check.

Comparing the *derived decision* — not the raw minutes with a wide
tolerance — matters here specifically because of the threshold boundary:
two validators could both read "somewhere around 175–185 minutes late" and
call that "close enough," while actually landing on opposite sides of a
180-minute threshold. Deciding first, then comparing the decision, is the
["derive status from variable data"](https://docs.genlayer.com/developers/intelligent-contracts/crafting-prompts#compare-derived-status-not-raw-data)
pattern GenLayer's own docs recommend for exactly this reason. A
malformed model response, or a status page that can't be fetched at all,
raises outright rather than defaulting to a decision either way — see
`test_malformed_oracle_response_is_rejected` and
`test_unfetchable_status_page_is_rejected`.

## Underwriting guardrails

None of this tries to be a real actuarial model, but it isn't naive
either:

- **`min_premium_bps`** — the premium must be worth at least this
  fraction of the requested payout (default 5%). Stops someone buying
  $1 of premium for a $1,000 payout.
- **`max_payout_multiple`** — a payout can never be more than this many
  multiples of its own premium (default 10x), an absolute leverage cap
  independent of the percentage rule above. Whichever of the two binds
  tighter for a given admin configuration is the one that applies —
  see `test_buy_policy_rejects_premium_below_min_bps` for how to exercise
  the percentage rule specifically when the multiple is looser.
- **`max_single_policy_exposure_bps`** — a single policy's payout can
  never reserve more than this fraction of the *entire* pool (default
  20%, hard-capped at 50% regardless of what the admin sets), so one
  policy can't concentrate all of the pool's risk.
- **Available capacity, not total balance, backs new policies.**
  `total_pool_balance - total_reserved_exposure` is checked at purchase
  time.
- **Exits are priced net of open liabilities.** `withdraw_liquidity` pays
  `shares × (pool balance − reserved exposure) / total shares`, and the last
  shares can't be withdrawn while any policy still reserves capital.
  Otherwise an LP who already knows a claim will pay (say, the flight is
  already cancelled) could leave at full NAV and push the loss onto the LPs
  who stay. Deposits are priced on the gross balance, so neither entering
  nor leaving can extract value from the other LPs; the cost of moving
  while policies are open falls on the mover, and the held-back amount stays
  in the pool for the remaining LPs. `get_lp_position` returns both `value`
  (gross) and `withdrawable_value` (what a withdrawal would pay right now).
  A withdrawal queue would be the next step if that trade-off is too blunt.
- **`__receive__` is deliberately left undefined.** A bare GEN transfer
  with no method name is rejected outright by GenVM's own dispatch rules
  (see [Special Methods](https://docs.genlayer.com/developers/intelligent-contracts/features/special-methods)),
  so `total_pool_balance` can never silently drift from the contract's
  real balance via an unaccounted "donation" that minted no shares.
- **Anyone can resolve or expire a policy**, not just the policyholder or
  an LP — mirroring GenLayer's own staking contracts, where "claims are
  permissionless — anyone can trigger them." A policyholder who forgets
  doesn't lock up the pool's capital forever, and doesn't need to be the
  one who calls in to collect, either.

## Scaling the two views that touch every policy

Two operations naturally want to look across every policy a wallet or the
platform has ever seen: "what has this wallet bought?" and "stop tracking
this policy as active." Both are backed by an auxiliary index instead of
a scan, specifically so their cost doesn't grow with the platform's total
policy count:

- **`get_wallet_policies`** is backed by `wallet_policy_count` (how many
  policies a wallet has ever bought) and `wallet_policy_index` (an
  append-only `"{wallet}:{i}" -> policy_id` map). `buy_policy` writes one
  new entry and bumps the counter; the view then reads exactly that
  wallet's own `count` entries — cost scales with *that wallet's* history,
  never with every policy on the platform.
- **`_remove_active`**, called from both `resolve_policy` and
  `expire_policy`, is backed by `active_policy_index` (`policy_id ->` its
  current position in `active_policy_ids`). Removal becomes a direct
  lookup plus a standard swap-and-pop — O(1) instead of scanning the
  whole active list for the element's position — and the moved element's
  own index entry is updated in the same call, so a later removal never
  reads a stale slot. `test_active_policy_index_stays_correct_through_a_middle_removal`
  exercises this by removing from the middle of a 5-policy list and then
  independently removing the element that got swapped into that slot.

Neither of these was a security hole — no drain path, no way to move
capital out of the pool incorrectly — but both were a real scalability
problem: as written, `get_wallet_policies` scanned every policy the
platform had ever issued, and `_remove_active` scanned the entire active
list on every single `resolve_policy`/`expire_policy` call. At a few
dozen policies that's invisible; at thousands of policies (or thousands
of *active* policies sitting in the array `_remove_active` walks) it's
exactly the kind of unbounded loop that GenVM's own resource limits are
built to reject, turning routine settlement calls into failing
transactions. Both are fixed the same way: replace "scan to find it"
with "look up where it already is."

## Contract API

| Method | Type | Notes |
|---|---|---|
| `deposit_liquidity()` | payable write | mints shares, 1:1 on bootstrap, pro-rata after |
| `withdraw_liquidity(shares)` | write | blocked from touching capital reserved for active policies |
| `add_data_source(name, url_template)` | write, admin | template must be http(s) and contain `{FLIGHT}` + `{DATE}` |
| `set_data_source_active(id, active)` | write, admin | |
| `buy_policy(data_source_id, flight_code, flight_date, delay_threshold_minutes, payout_amount)` | payable write | premium = value sent; returns the policy id |
| `resolve_policy(policy_id)` | write | callable by anyone once `resolvable_at` (flight date + 30h) has passed |
| `expire_policy(policy_id)` | write | callable by anyone once the 30-day claim window lapses unresolved |
| `set_protocol_fee_bps` / `set_treasury` / `set_min_premium_bps` / `set_max_payout_multiple` / `set_max_single_policy_exposure_bps` / `set_min_deposit` / `set_paused` / `transfer_admin` | write, admin | protocol configuration |
| `get_policy(id)` / `get_data_source(id)` / `get_data_sources(limit)` | view | |
| `get_active_policies(limit)` | view | policies not yet resolved or expired |
| `get_wallet_policies(wallet)` | view | every policy a wallet has bought, any status; O(that wallet's own history) |
| `get_lp_position(wallet)` | view | shares owned + their current underlying GEN value |
| `get_audit_log(start, count)` / `get_stats()` | view | |

Run `genvm-lint schema contracts/sky_cover.py` for the machine-readable
version of this table.

## Running the tests

Same [`genlayer-test`](https://docs.genlayer.com/api-references/genlayer-test)
Direct Mode setup as GenLayer's own docs describe — the contract's actual
Python code runs in-process, no Docker, no GenVM, no network:

```bash
pip install -r requirements.txt
pytest tests/ -v
```

54 tests cover the full pool/policy lifecycle: share minting and
withdrawal (including the reserved-capital guard), every purchase
validation rule, all four resolution outcomes (paid on cancellation, paid
on threshold-or-beyond delay, declined on-time, declined below-threshold),
expiry, the admin surface, the equivalence-principle judging pattern
itself (a validator that agrees within tolerance, one that disagrees on a
different decision outright, and one that disagrees on minutes drifting
too far *despite* landing on the same decision), and the two index-map
regression tests described above, plus regression tests for the review
fixes below.

The first run downloads and caches the pinned GenVM release (`v0.2.12`,
matched to the `Depends` header in `sky_cover.py`) under
`~/.cache/gltest-direct/`; later runs are instant.

Static checks, if you have [`genvm-linter`](https://docs.genlayer.com/api-references/genlayer-linter)
installed:

```bash
genvm-lint check contracts/sky_cover.py       # AST safety + SDK validation
genvm-lint typecheck contracts/sky_cover.py   # pyright with the SDK preloaded
```

Both pass cleanly (0 errors, 0 warnings).

## Review fixes

A bug review of the first version found and fixed three issues (each has a
regression test that fails on the old code):

1. **Non-evidence could finalize a claim as `DECLINED`.** HTTP error pages
   and `"unknown"` answers used to become `no_pay` permanently, and only the
   first 4,000 characters of raw HTML were shown to the model.
2. **An informed LP could exit before a known payout** at full NAV, shifting
   the loss to the remaining LPs (see "Exits are priced net…" above).
3. **Flight code/date validation** used `^…$` with `.match` and `\d`, which
   accepted a trailing newline and non-ASCII digits. Now `fullmatch` with
   ASCII-only matching.

Known, intentionally not changed: the admin is a single trusted key that
curates data sources (a malicious admin could register a source they control
and buy policies against it — use a multisig/timelock in production);
`diverted` flights are treated as `no_pay`; page text is untrusted input to
the model (prompt injection is reduced by the admin-curated source list, not
eliminated).

## Honest limitations

- **This is a demonstration, not a licensed insurance product.** It
  carries no regulatory backing, no reinsurance, and no guarantee the
  pool has enough capital to cover every claim it's written — the
  concentration and capacity checks reduce that risk, they don't
  eliminate it if many correlated flights (say, one storm, one day) get
  insured against the same thin pool.
- **Flight-date granularity is a day, not an exact scheduled time.**
  Real flight data has an exact scheduled departure/arrival timestamp;
  this contract only takes a UTC calendar date, so its anti-adverse-
  selection rule is a blunt "must buy at least 6 hours before that UTC
  day begins" rather than "must buy before the specific flight's
  boarding time." A production version would want an aviation data API
  (FlightAware AeroAPI, AviationStack, etc.) supplying exact scheduled
  times, fetched the same way via `gl.nondet.web.request`.
- **The admin can add a bad or unreliable data source template.** The
  zero-placeholder and http(s) checks stop obviously wrong input, not a
  legitimate-looking template pointed at a low-quality or manipulable
  page. Choosing trustworthy sources is still an operational
  responsibility, not something the contract can fully verify on its own.
- **`get_active_policies` and `get_data_sources` are still plain scans**
  (of the active list and the data-source registry respectively), unlike
  `get_wallet_policies`. That's a deliberate choice, not an oversight:
  the active-policy count self-limits (every entry eventually resolves or
  expires and leaves the list) and the data-source registry is admin-only
  and expected to stay small — neither grows without bound the way "every
  policy ever bought platform-wide" does. If either ever became large
  enough to matter, the same counter-plus-indexed-map approach used for
  `get_wallet_policies` would apply directly.

  ## Deployed Contract (Studionet)

The SkyCover contract has been successfully manually deployed to the GenLayer Studionet for testing and evaluation.

- **Network:** Studionet (Chain ID: `61999`)
- **Contract Address:** `0xC668eF5d7e61414100E4631819d5aDf81C0c8b83`
- **Constructor Arguments Used:** `500` `500` `10` `2000` `100`

## License

MIT
