"""End-to-end smoke test for the bounty escrow flow.

Exercises:
  1. Owner creation auto-grants INITIAL_CREDIT_BALANCE (1000 CREDIT)
  2. select_bids on a paid bounty reserves funds (committed_balance += amount)
  3. fulfill_bounty must be called by the BIDDER, not the poster
  4. confirm_bounty_settlement transfers funds from poster → bidder
  5. cancel_bounty on a reserved bounty releases funds back to the poster
  6. Insufficient balance is rejected before any state changes

Runs against a fresh sqlite DB in /tmp so it doesn't touch dev/prod data.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Point at a fresh temp DB BEFORE importing the store module.
_TMP_DB = Path(tempfile.mkdtemp(prefix="payment_smoke_")) / "smoke.db"
os.environ["CLAW_DB_PATH"] = str(_TMP_DB)

# Make project root importable.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import store  # noqa: E402

# Override the DB path that store.py hardcoded at import time.
store.DB_PATH = _TMP_DB

from features.economy import store as economy  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def setup_owner(claw_runtime: str, name: str, owner_name: str, phone: str) -> tuple[str, str]:
    """Register a lobster, bind it to a freshly-created phone-bound owner.

    Returns (claw_id, owner_id). The owner is auto-granted 1000 CREDIT by
    economy.get_or_create_owner_by_phone.
    """
    lobster, _, _ = store.register_lobster(
        runtime_id=claw_runtime,
        name=name,
        owner_name=owner_name,
        connection_request_policy=store.DEFAULT_CONNECTION_REQUEST_POLICY,
        collaboration_policy=store.DEFAULT_COLLABORATION_POLICY,
        official_lobster_policy=store.DEFAULT_OFFICIAL_LOBSTER_POLICY,
        session_limit_policy=store.DEFAULT_SESSION_LIMIT_POLICY,
    )
    claw_id = str(lobster["claw_id"])
    owner = economy.get_or_create_owner_by_phone(phone, real_name=owner_name)
    owner_id = str(owner["id"])
    economy.link_lobster_to_owner(str(lobster["id"]), owner_id)
    return claw_id, owner_id


def main() -> int:
    section("Setup: init schema, create 2 phone-bound owners with lobsters")
    store.init_db()
    economy.ensure_economy_tables()

    poster_claw, poster_owner = setup_owner(
        "smoke-poster", "测试发布者龙虾", "测试发布者", "13800000001"
    )
    bidder_claw, bidder_owner = setup_owner(
        "smoke-bidder", "测试投标者龙虾", "测试投标者", "13800000002"
    )
    print(f"  poster: {poster_claw} owner={poster_owner}")
    print(f"  bidder: {bidder_claw} owner={bidder_owner}")

    section("Signup grant: each new owner starts with 1000 CREDIT, fully available")
    p_state = economy.get_account_state(poster_owner)
    b_state = economy.get_account_state(bidder_owner)
    check("poster.credit_balance == 1000", p_state["credit_balance"] == 1000, str(p_state))
    check("poster.committed_balance == 0", p_state["committed_balance"] == 0)
    check("poster.available_balance == 1000", p_state["available_balance"] == 1000)
    check("bidder.credit_balance == 1000", b_state["credit_balance"] == 1000, str(b_state))

    section("Happy path: post bounty (300), bid, select → escrow reserve")
    bounty = store.create_bounty(
        poster_claw_id=poster_claw,
        title="帮我翻译一段合同",
        description="一份英文 NDA",
        tags="translation,legal",
        bidding_window="1h",
        credit_amount=300,
    )
    bounty_id = str(bounty["id"])
    check("bounty has credit_amount=300", int(bounty["credit_amount"]) == 300)

    _, bid = store.bid_bounty(bounty_id=bounty_id, bidder_claw_id=bidder_claw, pitch="我可以")
    bid_id = str(bid["id"])

    bounty_after_select, selected_bid, invocation = store.select_bids(
        bounty_id=bounty_id, poster_claw_id=poster_claw, bid_ids=[bid_id]
    )
    check("select returned single bid", str(selected_bid["id"]) == bid_id)
    check("select returned an invocation", invocation is not None)
    check("invocation.settlement_status == 'reserved'",
          (invocation or {}).get("settlement_status") == "reserved")
    check("bounty.invocation_id is set",
          bool(bounty_after_select["invocation_id"]))
    check("bounty.status == 'assigned'", str(bounty_after_select["status"]) == "assigned")

    p_state = economy.get_account_state(poster_owner)
    b_state = economy.get_account_state(bidder_owner)
    check("after reserve: poster.credit_balance still 1000 (not yet debited)",
          p_state["credit_balance"] == 1000, str(p_state))
    check("after reserve: poster.committed_balance == 300",
          p_state["committed_balance"] == 300)
    check("after reserve: poster.available_balance == 700",
          p_state["available_balance"] == 700)
    check("after reserve: bidder.credit_balance still 1000",
          b_state["credit_balance"] == 1000, str(b_state))

    section("fulfill_bounty must be called by the BIDDER (auth change)")
    try:
        store.fulfill_bounty(bounty_id=bounty_id, bidder_claw_id=poster_claw)
        check("poster cannot fulfill", False, "expected ValueError, got success")
    except ValueError as exc:
        check("poster cannot fulfill", True, f"got expected error: {exc}")

    fulfilled = store.fulfill_bounty(bounty_id=bounty_id, bidder_claw_id=bidder_claw)
    check("bidder fulfill → status='fulfilled'",
          str(fulfilled["status"]) == "fulfilled")
    p_state = economy.get_account_state(poster_owner)
    check("after fulfill (no settle yet): poster.committed still 300",
          p_state["committed_balance"] == 300)

    section("confirm_bounty_settlement: poster confirms → funds transfer")
    settled_bounty, settled_invocation = store.confirm_bounty_settlement(
        bounty_id=bounty_id, poster_claw_id=poster_claw
    )
    check("bounty.status == 'settled'", str(settled_bounty["status"]) == "settled")
    check("invocation.settlement_status == 'settled'",
          (settled_invocation or {}).get("settlement_status") == "settled")

    p_state = economy.get_account_state(poster_owner)
    b_state = economy.get_account_state(bidder_owner)
    check("after settle: poster.credit_balance == 700",
          p_state["credit_balance"] == 700, str(p_state))
    check("after settle: poster.committed_balance == 0",
          p_state["committed_balance"] == 0)
    check("after settle: poster.available_balance == 700",
          p_state["available_balance"] == 700)
    check("after settle: bidder.credit_balance == 1300",
          b_state["credit_balance"] == 1300, str(b_state))
    check("after settle: bidder.available_balance == 1300",
          b_state["available_balance"] == 1300)

    section("Cancel path: post → bid → select → cancel → funds released")
    bounty2 = store.create_bounty(
        poster_claw_id=poster_claw,
        title="临时需求,马上要撤",
        bidding_window="1h",
        credit_amount=200,
    )
    bounty2_id = str(bounty2["id"])
    _, bid2 = store.bid_bounty(bounty_id=bounty2_id, bidder_claw_id=bidder_claw)
    store.select_bids(bounty_id=bounty2_id, poster_claw_id=poster_claw, bid_ids=[str(bid2["id"])])

    p_state = economy.get_account_state(poster_owner)
    check("after second reserve: committed == 200",
          p_state["committed_balance"] == 200, str(p_state))
    check("after second reserve: available == 500",
          p_state["available_balance"] == 500)

    cancelled = store.cancel_bounty(bounty_id=bounty2_id, poster_claw_id=poster_claw)
    check("cancelled bounty.status == 'cancelled'",
          str(cancelled["status"]) == "cancelled")

    p_state = economy.get_account_state(poster_owner)
    check("after cancel: committed back to 0",
          p_state["committed_balance"] == 0, str(p_state))
    check("after cancel: available back to 700",
          p_state["available_balance"] == 700)
    check("after cancel: credit_balance unchanged at 700",
          p_state["credit_balance"] == 700)

    section("Insufficient balance: try to reserve more than available")
    big_bounty = store.create_bounty(
        poster_claw_id=poster_claw,
        title="超贵的需求",
        bidding_window="1h",
        credit_amount=99999,
    )
    _, big_bid = store.bid_bounty(
        bounty_id=str(big_bounty["id"]), bidder_claw_id=bidder_claw
    )
    try:
        store.select_bids(
            bounty_id=str(big_bounty["id"]),
            poster_claw_id=poster_claw,
            bid_ids=[str(big_bid["id"])],
        )
        check("oversized reserve rejected", False, "expected ValueError")
    except ValueError as exc:
        check("oversized reserve rejected", True, f"got expected: {exc}")
    # And the bounty should still be in 'open' state (or 'bidding'), not 'assigned'.
    after = store.get_bounty(str(big_bounty["id"]))
    check("failed reserve left bounty unassigned",
          str(after["status"]) in ("open", "bidding"), f"status={after['status']}")
    p_state = economy.get_account_state(poster_owner)
    check("failed reserve did not move money",
          p_state["committed_balance"] == 0 and p_state["credit_balance"] == 700,
          str(p_state))

    section(f"Result: {PASS} pass, {FAIL} fail")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
