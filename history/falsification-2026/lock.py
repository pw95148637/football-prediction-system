"""Estimate lock — the anchoring guard (PROTOCOL.md section 3).

`create` stamps a UTC time and a sha256 over the estimate payload and refuses
to overwrite: locks are immutable. `amend` never touches the original — it
writes a new version file that must cite real new information (market price
is NOT new information) and carries the original's hash. score.py audits
lock time against the first odds fetch per match.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LOCKS_DIR = Path(__file__).parent / "locks"

REQUIRED = ["match_id", "teams", "kickoff_utc", "mode", "probs_90",
            "qualify_home", "lambdas", "inputs", "rationale",
            "invalidation", "sources"]


def _canonical_sha(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _check_pre_kickoff(payload, action):
    """Post-kickoff locks/amends are never legitimate: a lock after kickoff
    can see the match state (the m92 lesson — '賽後補鎖又有看比分之虞,連
    M-only 都不誠實'), and a post-kickoff amend is post-hoc adjustment.
    Enforced mechanically since 2026-07-08 (Architecture Review Phase 0)."""
    ko = datetime.fromisoformat(payload["kickoff_utc"])
    if ko.tzinfo is None:
        ko = ko.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if now >= ko:
        raise ValueError(
            f"REFUSED {action}: kickoff {payload['kickoff_utc']} has passed "
            f"(now {now.isoformat()}). Post-kickoff locks/amends are never "
            f"honest — record the match as MISSING instead (m92 precedent).")


def _validate(payload):
    missing = [k for k in REQUIRED if k not in payload]
    if missing:
        raise ValueError(f"missing fields: {missing}")
    p = payload["probs_90"]
    vec = [p["home"], p["draw"], p["away"]]
    if abs(sum(vec) - 1.0) > 1e-6:
        raise ValueError(f"probs_90 sums to {sum(vec):.6f}, not 1")
    if any(not (0.0 < x < 1.0) for x in vec):
        raise ValueError("probs_90 entries must be in (0,1)")
    if not (0.0 < payload["qualify_home"] < 1.0):
        raise ValueError("qualify_home must be in (0,1)")
    if any(v <= 0 for v in payload["lambdas"].values()):
        raise ValueError("lambdas must be positive")
    if not payload["sources"]:
        raise ValueError("sources must be non-empty (item + source + as_of each)")
    j = payload.get("j")
    if j is not None:  # instrument J (LLM synthesis), optional but validated
        jv = [j["probs_90"]["home"], j["probs_90"]["draw"], j["probs_90"]["away"]]
        if abs(sum(jv) - 1.0) > 1e-6:
            raise ValueError(f"j.probs_90 sums to {sum(jv):.6f}, not 1")
        if any(not (0.0 < x < 1.0) for x in jv):
            raise ValueError("j.probs_90 entries must be in (0,1)")
        if not (0.0 < j["qualify_home"] < 1.0):
            raise ValueError("j.qualify_home must be in (0,1)")
        if not j.get("basis"):
            raise ValueError("j.basis must record what J consulted and weighed")


def create_lock(payload, locks_dir=LOCKS_DIR):
    _validate(payload)
    _check_pre_kickoff(payload, "create")
    locks_dir.mkdir(parents=True, exist_ok=True)
    path = locks_dir / f"{payload['match_id']}.json"
    if path.exists():
        raise FileExistsError(
            f"{path} already exists — locks are immutable; use amend")
    doc = {
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "version": 1,
        "payload": payload,
        "sha256": _canonical_sha(payload),
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def amend_lock(match_id, payload, reason, locks_dir=LOCKS_DIR):
    _validate(payload)
    _check_pre_kickoff(payload, "amend")
    base = locks_dir / f"{match_id}.json"
    if not base.exists():
        raise FileNotFoundError(f"no original lock for {match_id}")
    if not reason or not reason.strip():
        raise ValueError("amend requires a concrete new-information reason")
    original = json.loads(base.read_text(encoding="utf-8"))
    version = 2
    while (locks_dir / f"{match_id}.v{version}.json").exists():
        version += 1
    doc = {
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "amend_reason": reason,
        "original_sha256": original["sha256"],
        "payload": payload,
        "sha256": _canonical_sha(payload),
    }
    path = locks_dir / f"{match_id}.v{version}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def verify_lock(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    return _canonical_sha(doc["payload"]) == doc["sha256"], doc


def main(argv=None):
    ap = argparse.ArgumentParser(description="Estimate lock (anchoring guard)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="lock a Phase-1 estimate (immutable)")
    c.add_argument("payload_json", help="path to estimate payload JSON")

    v = sub.add_parser("verify", help="recompute a lock file's hash")
    v.add_argument("lock_file")

    a = sub.add_parser("amend", help="new version citing real new info")
    a.add_argument("match_id")
    a.add_argument("payload_json")
    a.add_argument("--reason", required=True,
                   help="the concrete new information (market price is not)")

    args = ap.parse_args(argv)
    if args.cmd == "create":
        payload = json.loads(Path(args.payload_json).read_text(encoding="utf-8"))
        path = create_lock(payload)
        print(f"LOCKED {path}")
    elif args.cmd == "verify":
        ok, doc = verify_lock(args.lock_file)
        print(f"{'OK' if ok else 'HASH MISMATCH'}  "
              f"v{doc['version']} locked_at={doc['locked_at']}")
        sys.exit(0 if ok else 1)
    elif args.cmd == "amend":
        payload = json.loads(Path(args.payload_json).read_text(encoding="utf-8"))
        path = amend_lock(args.match_id, payload, args.reason)
        print(f"AMENDED -> {path} (original untouched)")


if __name__ == "__main__":
    main()
