"""Services: a real HTTP service and database, formalized as a state machine and tested
with request sequences (the LLM is scripted; the service, database and Lean are real)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from larch.config import Config

WALLET_OK = '''\
"""Store-credit wallets over HTTP (stdlib only).

    POST /wallets/{customer}/deposit   {"amount_cents": int > 0}  -> 200 {"balance_cents"} | 422
    POST /wallets/{customer}/withdraw  {"amount_cents": int > 0}  -> 200 {"balance_cents"} | 402 if short | 422
    GET  /wallets/{customer}                                       -> 200 {"balance_cents"}
"""
import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ["DATABASE_URL"][len("sqlite:///"):]


def db():
    c = sqlite3.connect(DB, isolation_level=None)
    c.execute("CREATE TABLE IF NOT EXISTS wallets (customer TEXT PRIMARY KEY, balance INTEGER NOT NULL)")
    return c


def balance(c, who):
    row = c.execute("SELECT balance FROM wallets WHERE customer = ?", (who,)).fetchone()
    return row[0] if row else 0


def put(c, who, value):
    c.execute("INSERT INTO wallets VALUES (?, ?) ON CONFLICT(customer) DO UPDATE SET balance = excluded.balance", (who, value))


def deposit(who, amount):
    if not isinstance(amount, int) or amount <= 0:
        return 422, {"detail": "amount_cents must be a positive integer"}
    c = db()
    put(c, who, balance(c, who) + amount)
    return 200, {"customer": who, "balance_cents": balance(c, who)}


def withdraw(who, amount):
    if not isinstance(amount, int) or amount <= 0:
        return 422, {"detail": "amount_cents must be a positive integer"}
    c = db()
    if balance(c, who) < amount:
        return 402, {"detail": "insufficient store credit"}
    put(c, who, balance(c, who) - amount)
    return 200, {"customer": who, "balance_cents": balance(c, who)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parts = self.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "wallets":
            return self.reply(200, {"customer": parts[1], "balance_cents": balance(db(), parts[1])})
        self.reply(404, {"detail": "not found"})

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if len(parts) == 3 and parts[0] == "wallets" and parts[2] in ("deposit", "withdraw"):
            op = deposit if parts[2] == "deposit" else withdraw
            return self.reply(*op(parts[1], body.get("amount_cents")))
        self.reply(404, {"detail": "not found"})


if __name__ == "__main__":
    db().close()
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
'''
# Overdraft bug: an empty wallet is not checked (balance 0 is treated as "no wallet yet").
WALLET_BUG = WALLET_OK.replace("    if balance(c, who) < amount:\n", "    if 0 < balance(c, who) < amount:\n")
FIXED_WITHDRAW = WALLET_OK[WALLET_OK.index("def withdraw"):WALLET_OK.index("\n\n\nclass Handler")] + "\n"

MODEL = '''\
structure State where
  wallets : List (String × Int)
  deriving Repr, DecidableEq

def bal (ws : List (String × Int)) (a : String) : Int :=
  match ws with
  | [] => 0
  | (k, v) :: rest => if k = a then v else bal rest a

def setBal (ws : List (String × Int)) (a : String) (v : Int) : List (String × Int) :=
  (a, v) :: ws.filter (fun p => p.1 ≠ a)

def init : Option State := some ⟨[]⟩

def op_deposit (s : State) (customer : String) (amount : Int) : Option (State × (Nat × Option Int)) :=
  if amount ≤ 0 then some (s, (422, none))
  else some (⟨setBal s.wallets customer (bal s.wallets customer + amount)⟩, (200, some (bal s.wallets customer + amount)))

def op_withdraw (s : State) (customer : String) (amount : Int) : Option (State × (Nat × Option Int)) :=
  if amount ≤ 0 then some (s, (422, none))
  else if bal s.wallets customer < amount then some (s, (402, none))
  else some (⟨setBal s.wallets customer (bal s.wallets customer - amount)⟩, (200, some (bal s.wallets customer - amount)))

def op_get_wallet (s : State) (customer : String) : Option (State × (Nat × Option Int)) :=
  some (s, (200, some (bal s.wallets customer)))
'''


def formalization() -> dict:
    who = {"name": "customer", "lean_type": "String", "in": "path", "key": "customer"}
    amt = {"name": "amount", "lean_type": "Int", "in": "body", "key": "amount_cents"}
    field = [{"key": "balance_cents", "lean_type": "Int", "opaque": False}]
    return {
        "understanding": "Wallet balances per customer; a withdrawal larger than the balance is refused with 402.",
        "operations": [
            {"name": "deposit", "verb": "POST", "path": "/wallets/{customer}/deposit", "params": [who, amt], "response_fields": field},
            {"name": "withdraw", "verb": "POST", "path": "/wallets/{customer}/withdraw", "params": [who, amt], "response_fields": field},
            {"name": "get_wallet", "verb": "GET", "path": "/wallets/{customer}", "params": [who], "response_fields": field},
        ],
        "model": MODEL,
        "invariants": [{"name": "never_negative", "english": "No wallet balance is ever negative.",
                        "lean": "∀ p ∈ s.wallets, 0 ≤ p.2", "contract": 0}],
        "operation_contracts": [
            {"name": "short_withdraw_refused", "english": "Withdrawing more than the balance is refused with 402 and changes nothing.",
             "operation": "withdraw", "lean": "0 < amount → bal s.wallets customer < amount → result = some (s, (402, none))",
             "contract": 1},
        ],
        "input_generator": (
            "def strategy(st):\n"
            "    who = st.sampled_from(['a', 'b'])\n"
            "    amt = st.integers(-1, 6)\n"
            "    return {'init': st.just(()), 'deposit': st.tuples(who, amt), 'withdraw': st.tuples(who, amt),\n"
            "            'get_wallet': st.tuples(who)}"
        ),
        "exhaustive_domains": json.dumps({
            "init": [[]], "deposit": [["a", 2]], "withdraw": [["a", 1], ["a", 3]], "get_wallet": [["a"]],
        }),
        "notes": "",
    }


def _lean():
    from larch.lean.toolchain import ToolchainError, find_toolchain

    try:
        return find_toolchain()
    except ToolchainError:
        return None


needs_lean = pytest.mark.skipif(_lean() is None, reason="pinned Lean toolchain not installed")


def _llm(fix: str | None = None):
    from larch.llm.base import LLM, Ledger
    from larch.llm.providers import FakeProvider

    def respond(req):
        if req.stage == "formalize":
            return formalization()
        if req.stage == "adjudicate":
            return {"verdict": "implementation_bug", "explanation": "an empty wallet was overdrawn"}
        if req.stage == "fix":
            return {"explanation": "check every wallet", "file": "wallet_api.py", "function": "withdraw",
                    "fixed_function": fix or ""}
        return "```lean\n-- nothing\n```"

    return LLM(FakeProvider(respond), Ledger())


def _subject(tmp_path: Path, src: str):
    from larch import contracts

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "wallet_api.py").write_text(src)
    (tmp_path / "LARCH.md").write_text(
        "# Contracts\n\n## service wallets\n"
        f"start: {sys.executable} wallet_api.py {{port}}\ndatabase: sqlite\nsource: wallet_api.py\n"
        "- No wallet balance is ever negative.\n"
        "- Withdrawing more than the balance is refused and changes nothing.\n"
    )
    return contracts.load(tmp_path / "LARCH.md").subjects[0]


def _cfg(tmp_path: Path) -> Config:
    return Config(auto_approve=True, sequences=150, service_mutants=3, artifacts=str(tmp_path / "runs"))


def test_service_subject_and_http_impl(tmp_path):
    from larch.engine.http_impl import HttpImpl
    from larch.services import start_service

    subject = _subject(tmp_path, WALLET_OK)
    assert subject.kind == "service" and subject.label == "service wallets"
    assert subject.settings["database"] == "sqlite"
    svc = start_service("wallets", subject.settings, tmp_path, tmp_path / "work")
    try:
        ops = {
            "deposit": {"verb": "POST", "path": "/wallets/{customer}/deposit", "has_body": True,
                        "params": [{"name": "customer", "in": "path", "key": "customer"},
                                   {"name": "amount", "in": "body", "key": "amount_cents"}],
                        "fields": [{"key": "balance_cents"}]},
        }
        impl = HttpImpl({"impl": {"service": svc.handle().to_json(), "operations": ops}})
        assert impl.new([], 1.0)["status"] == "ok"
        assert impl.invoke("deposit", ["a b", 5], 1.0)["value"] == (200, 5)
        assert impl.invoke("deposit", ["a b", 2], 1.0)["value"] == (200, 7)
        assert impl.invoke("deposit", ["a b", 0], 1.0)["value"] == (422, None)
        impl.new([], 1.0)  # a reset empties the database
        assert impl.invoke("deposit", ["a b", 1], 1.0)["value"] == (200, 1)
    finally:
        svc.stop()


@needs_lean
def test_correct_service_agrees_with_model(tmp_path):
    from larch.engine.service_session import verify_service

    subject = _subject(tmp_path, WALLET_OK)
    cfg = _cfg(tmp_path)
    cfg.run_mutation = False
    report = verify_service(subject, cfg, llm=_llm())
    assert report.kind == "service"
    assert report.verdict in ("passed", "partial"), (report.error, report.headline, report.warnings)
    assert report.drt["disagreements"] == 0 and report.drt["valid"] > 100
    assert report.drt["exhaustive_cases"] > 10
    assert next(s for s in report.specs if s.name == "short_withdraw_refused").proof.status == "proved"


@needs_lean
def test_overdraft_found_and_fix_validated_on_a_copy(tmp_path):
    from larch.engine.service_session import verify_service

    subject = _subject(tmp_path, WALLET_BUG)
    report = verify_service(subject, _cfg(tmp_path), llm=_llm(fix=FIXED_WITHDRAW))
    assert report.verdict == "bug", (report.error, report.headline)
    f0 = report.findings[0]
    assert "withdraw" in f0.args_repr and f0.args_repr.count("\n") <= 3
    assert f0.fix and f0.fix.validated, f0.fix and f0.fix.validation
    assert f0.fix.file.endswith("wallet_api.py") and "0 < balance" in f0.fix.diff
    assert (tmp_path / "wallet_api.py").read_text() == WALLET_BUG  # the working tree is untouched
    assert report.mutation is not None


def test_failed_variant_start_leaves_shared_database_running(tmp_path):
    """A mutant that cannot start must not stop the database the other runs share."""
    from larch.services import Database, ServiceError, start_service

    stopped = []

    class SharedDB(Database):
        def stop(self) -> None:
            stopped.append(True)

    db = SharedDB("none", "", "none")
    with pytest.raises(ServiceError):
        start_service("x", {"start": f"{sys.executable} -c 'raise SystemExit(1)' {{port}}"}, tmp_path, tmp_path / "w",
                      database=db, startup_timeout=5)
    assert stopped == []


# A second, independent bug: a customer who never deposited gets 404 instead of a zero balance.
WALLET_TWO_BUGS = WALLET_BUG.replace(
    '            return self.reply(200, {"customer": parts[1], "balance_cents": balance(db(), parts[1])})',
    '            c = db()\n'
    '            if c.execute("SELECT 1 FROM wallets WHERE customer = ?", (parts[1],)).fetchone() is None:\n'
    '                return self.reply(404, {"detail": "no wallet"})\n'
    '            return self.reply(200, {"customer": parts[1], "balance_cents": balance(c, parts[1])})',
)


@needs_lean
def test_two_bugs_reported_separately_and_fix_for_one_accepted(tmp_path):
    from larch.engine.service_session import verify_service

    assert WALLET_TWO_BUGS != WALLET_BUG
    subject = _subject(tmp_path, WALLET_TWO_BUGS)
    cfg = _cfg(tmp_path)
    cfg.run_mutation = False
    report = verify_service(subject, cfg, llm=_llm(fix=FIXED_WITHDRAW))
    assert report.verdict == "bug", (report.error, report.headline)
    calls = {f.args_repr.splitlines()[-1].split("(")[0] for f in report.findings}
    assert {"service_wallets.withdraw", "service_wallets.get_wallet"} <= calls, [f.args_repr for f in report.findings]
    fix = next(f.fix for f in report.findings if f.fix)
    assert fix.validated and "other disagreement" in fix.validation, fix.validation
