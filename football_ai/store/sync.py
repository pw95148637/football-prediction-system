"""Keep the git-tracked ledger honest when more than one machine writes it.

WHY THIS EXISTS (2026-10-06). `io.py`'s "single writer" means one CODE PATH
writes the store files, not one machine. Once the ledger is edited from cloud
sessions and from an unattended hourly job, two failures appear that no
module-level test can see:

  1. A STALE CHECKOUT FAILS OPEN. The contamination guards (`amend_lock`,
     `ops.run_j`) read the LOCAL copy of the odds file. If the hourly job has
     already priced a fixture and this checkout has not pulled, the guards see
     no price, a J gets written, and that J is permanently contaminated --
     with nothing in the program able to notice. `assert_current()` refuses
     to continue while origin holds ledger changes this checkout lacks.
  2. NOTHING PERSISTS A CLOUD CONTAINER'S WRITES. A record that is not pushed
     dies with the container. `publish()` commits the ledger, rebases onto
     origin and pushes; it never hand-merges a JSONL file (two writers that
     appended to the same file is a conflict to stop on, not to resolve).

WHO WRITES WHICH FILE keeps rebases conflict-free in practice: the hourly job
writes `odds`, `results` and the results snapshot; interactive sessions write
`locks`, `judgments`, `evidence` and the rest. Different files never conflict.

This module imports nothing from the package (the store layer's rule).
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORE_PATH = "football_ai/store/data"
# What `publish` commits: the ledger, plus the price-free results snapshot the
# unattended job refreshes (data/football_data_co_uk.py). Only STORE_PATH is
# compared by `assert_current`; a stale snapshot cannot contaminate anything.
PUBLISH_PATHS = (STORE_PATH, "football_ai/data/results_snapshot")
LEDGER_BRANCH = os.environ.get("FOOTBALL_AI_LEDGER_BRANCH", "main")
# Deliberate escape hatch (offline work, a test). Never set it to get past a
# refusal you have not understood: the refusal is the contamination guard.
SKIP_ENV = "FOOTBALL_AI_SKIP_SYNC_CHECK"


class SyncError(RuntimeError):
    """The checkout cannot be trusted to see the ledger as it really is."""


def _git(args, root, timeout=120):
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                          text=True, timeout=timeout)


def _has_origin(root):
    r = _git(["remote", "get-url", "origin"], root)
    return r.returncode == 0


def _is_repo(root):
    r = _git(["rev-parse", "--is-inside-work-tree"], root)
    return r.returncode == 0 and r.stdout.strip() == "true"


def current_branch(root=None):
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], root or ROOT)
    return r.stdout.strip() if r.returncode == 0 else None


def assert_current(root=None, branch=None):
    """Raise SyncError unless this checkout has every ledger change that
    origin/<branch> has. Returns a small dict describing what was checked.

    Not a git checkout, or no `origin` remote: nothing to compare with, so
    nothing is claimed (`checked: False`). An origin that cannot be reached is
    a REFUSAL, not a pass: a check that silently passes offline is the failure
    this function exists to stop.
    """
    root = Path(root or ROOT)
    branch = branch or LEDGER_BRANCH
    if os.environ.get(SKIP_ENV) == "1":
        return {"checked": False, "why": f"{SKIP_ENV}=1"}
    if not _is_repo(root) or not _has_origin(root):
        return {"checked": False, "why": "not a git checkout with an origin"}
    f = _git(["fetch", "--quiet", "origin", branch], root, timeout=60)
    if f.returncode != 0:
        raise SyncError(
            f"REFUSED: cannot reach origin to confirm this checkout holds the "
            f"latest ledger ({f.stderr.strip()[:200]}). The contamination "
            f"guards read the local odds file; a stale one lets a J through "
            f"after a price already exists. Retry, or `git pull` and set "
            f"{SKIP_ENV}=1 only if you know nothing else writes the ledger.")
    d = _git(["diff", "--name-only", f"HEAD...origin/{branch}", "--", STORE_PATH],
             root)
    if d.returncode != 0:
        raise SyncError(f"REFUSED: could not compare with origin/{branch}: "
                        f"{d.stderr.strip()[:200]}")
    behind = [x for x in d.stdout.splitlines() if x.strip()]
    if behind:
        names = ", ".join(sorted(Path(x).name for x in behind))
        raise SyncError(
            f"REFUSED: origin/{branch} has ledger changes this checkout does "
            f"not ({names}). Someone else wrote the ledger (typically the "
            f"hourly job storing prices); acting on this copy would bypass "
            f"the contamination guards. `git pull --rebase origin {branch}` "
            f"first.")
    return {"checked": True, "branch": branch}


def _is_ledger_path(path):
    return any(path == x or path.startswith(x + "/") for x in PUBLISH_PATHS)


def publish(message, root=None, branch=None, retries=5):
    """Commit the ledger, rebase onto origin/<branch> and push. Returns a dict.

    Works from a named branch AND from a detached HEAD (a Claude Code cloud
    checkout can be either; 2026-10-06 one was detached). What it refuses is
    the thing that actually goes wrong: carrying unrelated unpushed commits to
    <branch> along with the ledger. It looks at what HEAD has that origin does
    not, and stops if any of it lies outside the ledger paths.

    On a rebase conflict it aborts the rebase and raises -- the local commit
    stays, nothing is lost, and the conflict is for a human to look at (never
    hand-merge a JSONL ledger).
    """
    root = Path(root or ROOT)
    branch = branch or LEDGER_BRANCH
    if not _is_repo(root) or not _has_origin(root):
        raise SyncError("publish needs a git checkout with an origin")
    f = _git(["fetch", "--quiet", "origin", branch], root, timeout=60)
    if f.returncode != 0:
        raise SyncError(f"cannot reach origin/{branch}: {f.stderr.strip()[:200]}")

    present = [x for x in PUBLISH_PATHS if (root / x).exists()]
    a = _git(["add", "--", *present], root)
    if a.returncode != 0:
        raise SyncError(f"git add failed: {a.stderr.strip()[:200]}")
    staged = _git(["diff", "--cached", "--quiet", "--", *present], root)
    committed = False
    if staged.returncode != 0:
        c = _git(["commit", "-q", "-m", message, "--", *present], root)
        if c.returncode != 0:
            raise SyncError(f"git commit failed: {c.stderr.strip()[:200]}")
        committed = True

    # everything HEAD carries beyond origin/<branch> will be published: it
    # must be ledger only
    d = _git(["diff", "--name-only", f"origin/{branch}...HEAD"], root)
    carried = [x for x in d.stdout.splitlines()
               if x.strip() and not _is_ledger_path(x)]
    if carried:
        raise SyncError(
            f"REFUSED: HEAD holds unpushed changes outside the ledger "
            f"({', '.join(carried[:5])}{' ...' if len(carried) > 5 else ''}); "
            f"publishing would push them to {branch} too. Push or discard "
            f"those first -- publish moves ledger files only.")

    for attempt in range(1, retries + 1):
        if attempt > 1:
            f = _git(["fetch", "--quiet", "origin", branch], root, timeout=60)
            if f.returncode != 0:
                raise SyncError(f"cannot reach origin/{branch}: "
                                f"{f.stderr.strip()[:200]}")
        # `rebase origin/<branch>` instead of `pull --rebase`: it also works
        # on a detached HEAD, and --autostash keeps unrelated uncommitted
        # work (a session mid-edit) out of the way
        r = _git(["rebase", "--autostash", "--quiet", f"origin/{branch}"],
                 root, timeout=180)
        if r.returncode != 0:
            _git(["rebase", "--abort"], root)
            raise SyncError(
                f"REFUSED: rebase onto origin/{branch} hit a conflict "
                f"({r.stderr.strip()[:300]}). Two writers appended to the same "
                f"ledger file. The rebase was aborted and your commit is still "
                f"local; do NOT hand-merge the JSONL -- look at which file "
                f"both sides touched.")
        ahead = _git(["rev-list", "--count", f"origin/{branch}..HEAD"], root)
        if ahead.returncode == 0 and ahead.stdout.strip() == "0":
            return {"published": False, "committed": committed,
                    "why": "nothing to publish", "attempts": attempt}
        push = _git(["push", "--quiet", "origin", f"HEAD:{branch}"], root,
                    timeout=180)
        if push.returncode == 0:
            sha = _git(["rev-parse", "--short", "HEAD"], root).stdout.strip()
            return {"published": True, "committed": committed, "commit": sha,
                    "attempts": attempt}
        # rejected (another writer got in between): fetch again and retry
    raise SyncError(f"push to origin/{branch} kept being rejected after "
                    f"{retries} attempts: {push.stderr.strip()[:300]}")


def _selfcheck():
    import shutil
    import tempfile

    if shutil.which("git") is None:
        return []  # no git on this machine: nothing to exercise
    fails = []
    old_skip = os.environ.pop(SKIP_ENV, None)

    def sh(args, cwd):
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git {args}: {r.stderr}")
        return r.stdout

    def write(repo, name, line):
        d = Path(repo) / STORE_PATH
        d.mkdir(parents=True, exist_ok=True)
        with (d / name).open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            origin = td / "origin.git"
            sh(["init", "--bare", "-q", "-b", "main", str(origin)], td)
            a, b = td / "a", td / "b"
            sh(["clone", "-q", str(origin), str(a)], td)
            for repo in (a,):
                sh(["config", "user.email", "t@example.com"], repo)
                sh(["config", "user.name", "t"], repo)
                sh(["checkout", "-q", "-B", "main"], repo)
            write(a, "odds.jsonl", '{"n":0}')
            write(a, "locks.jsonl", '{"n":0}')
            sh(["add", "-A"], a)
            sh(["commit", "-q", "-m", "init"], a)
            sh(["push", "-q", "-u", "origin", "main"], a)
            sh(["clone", "-q", str(origin), str(b)], td)
            sh(["config", "user.email", "t@example.com"], b)
            sh(["config", "user.name", "t"], b)

            if not assert_current(a).get("checked"):
                fails.append("an up-to-date checkout must pass the check")

            # another machine stores a price; this checkout must now refuse
            write(b, "odds.jsonl", '{"n":1}')
            if not publish("b: price", root=b)["published"]:
                fails.append("publish must push a new ledger line")
            try:
                assert_current(a)
                fails.append("a checkout behind origin on the ledger must be "
                             "refused (the stale-checkout contamination hole)")
            except SyncError as e:
                if "odds.jsonl" not in str(e):
                    fails.append(f"the refusal must name the file: {e}")
            sh(["pull", "-q", "--rebase", "origin", "main"], a)
            if not assert_current(a).get("checked"):
                fails.append("after pulling, the check must pass")

            # the price-free results snapshot travels with the ledger
            snap = b / "football_ai/data/results_snapshot"
            snap.mkdir(parents=True, exist_ok=True)
            (snap / "E0_2627.csv").write_text("Div,Date\nE0,01/01/2027\n",
                                              encoding="utf-8", newline="\n")
            if not publish("b: snapshot", root=b)["published"]:
                fails.append("publish must also push the results snapshot")
            sh(["pull", "-q", "--rebase", "origin", "main"], a)
            if not (a / "football_ai/data/results_snapshot/E0_2627.csv").exists():
                fails.append("the snapshot must reach the other machine")

            # different files from two machines: the rebase must just work
            write(b, "odds.jsonl", '{"n":2}')
            publish("b: price 2", root=b)
            write(a, "locks.jsonl", '{"n":1}')
            r = publish("a: lock", root=a)
            if not r["published"]:
                fails.append("publish must rebase over another writer's "
                             "different file and push")
            if sh(["rev-parse", "HEAD"], a) != sh(["rev-parse", "origin/main"], a):
                fails.append("after publish, local and origin must agree")

            # the same file from two machines: stop, abort, keep the commit
            sh(["pull", "-q", "--rebase", "origin", "main"], b)
            write(b, "locks.jsonl", '{"n":"b"}')
            publish("b: lock", root=b)
            write(a, "locks.jsonl", '{"n":"a"}')
            try:
                publish("a: lock too", root=a)
                fails.append("two writers appending to one file must stop, "
                             "not be merged by hand")
            except SyncError:
                pass
            if (a / ".git" / "rebase-merge").exists() or \
                    (a / ".git" / "rebase-apply").exists():
                fails.append("a failed publish must leave no rebase in progress")
            if "a: lock too" not in sh(["log", "--oneline", "-3"], a):
                fails.append("a failed publish must keep the local commit")

            # nothing to publish is not an error
            r = publish("noop", root=b)
            if r["published"]:
                fails.append("publish with nothing new must say so")

            # a detached HEAD (how a cloud checkout may arrive) must work
            sh(["pull", "-q", "--rebase", "origin", "main"], b)
            sh(["checkout", "-q", "--detach"], b)
            write(b, "odds.jsonl", '{"n":"detached"}')
            r = publish("b: detached", root=b)
            if not r["published"]:
                fails.append("publish must work from a detached HEAD")
            if sh(["rev-parse", "HEAD"], b) != sh(["rev-parse", "origin/main"], b):
                fails.append("after a detached publish, HEAD and origin/main "
                             "must agree")

            # unrelated unpushed work must not ride along to main
            (b / "football_ai").mkdir(exist_ok=True)
            (b / "football_ai" / "code.py").write_text("x = 1\n", encoding="utf-8")
            sh(["add", "football_ai/code.py"], b)
            sh(["commit", "-q", "-m", "unrelated code change"], b)
            write(b, "odds.jsonl", '{"n":"with-code"}')
            try:
                publish("b: should refuse", root=b)
                fails.append("publish must refuse while HEAD carries an "
                             "unpushed change outside the ledger")
            except SyncError as e:
                if "code.py" not in str(e):
                    fails.append(f"the refusal must name the stray file: {e}")

            # no origin: nothing to claim
            solo = td / "solo"
            sh(["init", "-q", str(solo)], td)
            if assert_current(solo).get("checked"):
                fails.append("a repo without an origin must not claim a check")

            # the override is explicit and visible
            os.environ[SKIP_ENV] = "1"
            if assert_current(a).get("checked"):
                fails.append(f"{SKIP_ENV}=1 must report the check as skipped")
    finally:
        os.environ.pop(SKIP_ENV, None)
        if old_skip is not None:
            os.environ[SKIP_ENV] = old_skip
    return fails


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Keep the git-tracked ledger consistent across machines.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="refuse unless this checkout holds origin's ledger")
    pp = sub.add_parser("publish", help="commit the ledger, rebase, push")
    pp.add_argument("-m", "--message", required=True)
    pp.add_argument("--branch", default=None)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "check":
            print(assert_current())
        else:
            print(publish(args.message, branch=args.branch))
    except SyncError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
