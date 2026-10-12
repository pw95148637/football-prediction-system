"""Isolated k-sample runner for instrument J (Architecture Review v3 §4.3).

WHY: in v2, J was the analyst-session's state of mind — unversionable,
unreproducible, and contaminated by anything the session had ever seen
(m97: seven characters of leaked market number retired a whole arm). Here
J = J_SPEC vN x validated evidence bundle x M — three artifacts, all
persisted. The interactive session ORCHESTRATES; it does not form J.

Honesty properties:
- The prompt is assembled from exactly three inputs: M, the bundle (which
  must already have passed evidence.schema.validate_bundle incl. the
  mechanical leak validator), and the frozen spec. Nothing else reaches
  the model — the conversation history of whoever invokes this cannot.
- k samples (default 3) are all persisted; the point estimate is the
  per-outcome median (renormalized) and the spread is reported as honest
  uncertainty. High dispersion = J has no stable view on this match.
- The spec's decomposition rule is enforced MECHANICALLY: a sample with an
  empty factors list whose probabilities differ from M is rejected — "no
  nameable mechanism => J = M" is a validator, not a vibe.
- LLM stochasticity and retrieval variance bound reproducibility; storing
  spec + bundle + all transcripts is the practical ceiling, stated plainly.

First LIVE use must smoke-test cli_llm() (the claude -p contract encodes
known Windows subscription-CLI gotchas but has not burned quota from
inside this package yet). The pre-registered WC2026 sample continues on
the v2 runbook; this runner's first real outing is the next window.

SMOKE-TESTED LIVE 2026-07-17 (m103_fra_eng, m104_esp_arg — parallel,
price-blind research trials, never written to locks/; see
reports/j_runner_trial_2026-07-17.md and reports/j_runner_trial_m10{3,4}.json
for the full transcripts). Found and fixed one real bug: cli_llm() passed
the bare string "claude" to subprocess.run()'s list form, which on Windows
raised FileNotFoundError — npm installs `claude` as claude.CMD/.ps1, and
CreateProcess (unlike a shell) does not do PATHEXT resolution. Fixed by
resolving the executable via shutil.which() first.

RETESTED 2026-07-17 (timeout + cwd-contamination follow-up; see
reports/j_runner_retest_2026-07-17.md). Found and fixed a third bug:
assemble_prompt() built m_block from probs_90/qualify_home/lambdas/inputs
only, silently dropping m_payload["teams"] even though every m_payload has
always carried it. Multiple samples (both the first trial and the retest)
explicitly noted having to guess home/away identity from Elo plausibility
or evidence content because "the input doesn't name the teams" — one
guessed backwards. Fixed by including teams in m_block. Retest also
confirmed the cwd-contamination concern is real and repeatable (2/3
wc2026-cwd runs attempted a blocked tool-use call vs. 0/3 neutral-cwd) —
fixed the same day: cli_llm() now defaults its subprocess `cwd` to
tempfile.gettempdir() instead of silently inheriting the caller's cwd, so a
call made from inside this project no longer runs the real subprocess from
inside it. Callers can still pass an explicit `cwd` if ever needed.
"""
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import tempfile
from pathlib import Path

SPEC_DIR = Path(__file__).parent / "j_spec"

# The specs a NEW run may use. One entry, deliberately: a self-contained
# round-robin spec. Composition is gone -- the old chain glued v1 + v2 +
# v2-rr into ~10KB in which v1 instructed the model to emit a knockout
# advancement probability and v2-rr then forbade it. The model had to hold
# and resolve that contradiction correctly or the validator rejected its
# sample. A spec should not require the instrument to notice it is being
# countermanded.
SPEC_LINEAGE = {
    "rr1": ["rr1"],
}

# Frozen specs kept ONLY so an old lock can be reproduced byte-for-byte.
# They are not options for new work and `load_spec` refuses them unless a
# caller explicitly says it is reproducing history. Listing them here
# rather than in SPEC_LINEAGE is the point: a reader looking for "which
# spec do I run" sees exactly one answer.
ARCHIVED_SPECS = {
    "v1": ["v1"],
    "v2": ["v1", "v2"],
    "v2-rr": ["v1", "v2", "v2-rr"],
}

CURRENT_SPEC = "rr1"


def load_spec(version=CURRENT_SPEC, allow_archived=False):
    """Load the spec text for `version`.

    Current specs are self-contained. Archived specs (the WC2026 v-chain)
    are refused unless `allow_archived=True`, which exists for reproducing
    an old lock and nothing else -- a new run that reaches for one is
    almost certainly a session that found the name lying around rather
    than a deliberate choice."""
    chain = SPEC_LINEAGE.get(version)
    if chain is None:
        if version in ARCHIVED_SPECS:
            if not allow_archived:
                raise ValueError(
                    f"j_spec {version!r} is ARCHIVED (WC2026 knockout window) "
                    f"and is not a choice for new work — use {CURRENT_SPEC!r}. "
                    f"Pass allow_archived=True only to reproduce an existing "
                    f"lock that recorded {version!r}.")
            chain = ARCHIVED_SPECS[version]
        else:
            raise ValueError(
                f"unknown j_spec version {version!r}; current specs are "
                f"{sorted(SPEC_LINEAGE)}")
    text = (SPEC_DIR / f"{chain[0]}.md").read_text(encoding="utf-8")
    for nxt in chain[1:]:
        delta = (SPEC_DIR / f"{nxt}.md").read_text(encoding="utf-8")
        text = (f"{text}\n\n---\n\n# The following supersedes"
                f"/extends everything above (spec {nxt}):\n\n{delta}")
    return version, text


def assemble_prompt(m_payload, claims, spec_text):
    """Render the three-and-only-three inputs into one prompt.

    `teams` is included when present so the isolated call knows which side
    is home/away by name, not just by elo_home/elo_away numbers. Confirmed
    missing live 2026-07-17: m_payload always carried a `teams` key
    (m103_fra_eng: home=France, m104_esp_arg: home=Spain) but this function
    silently dropped it, forcing every sample to guess team identity from
    Elo plausibility or evidence content — one m104 sample guessed backwards
    (home=Argentina) across both the first trial and the 2026-07-17 retest.
    See reports/j_runner_retest_2026-07-17.md."""
    m_block_data = {
        "teams": m_payload.get("teams", {}),
        "probs_90": m_payload["probs_90"],
        "lambdas": m_payload["lambdas"],
        "inputs": m_payload.get("inputs", {}),
    }
    # `qualify_home` is a KNOCKOUT concept (probability the home side
    # advances). A round-robin league fixture has no such market, and its M
    # payload carries no such key -- this used to be an unconditional
    # `m_payload["qualify_home"]`, i.e. a KeyError that made the whole J
    # instrument unrunnable for any league competition (2026-08-26).
    if "qualify_home" in m_payload:
        m_block_data["qualify_home"] = m_payload["qualify_home"]
    m_block = json.dumps(m_block_data, ensure_ascii=False, indent=2)
    ev_lines = []
    for i, c in enumerate(claims):
        ev_lines.append(f"[{i}] ({c['category']}, tier {c['source_tier']}, "
                        f"absorption={c['absorption']}) {c['claim']} "
                        f"— {c['source']}, accessed {c['access_time']}; "
                        f"absorption reasoning: {c['absorption_reason']}")
    return (f"{spec_text}\n\n---\n\n## INPUT M (mechanical baseline)\n\n"
            f"{m_block}\n\n## INPUT EVIDENCE (validated bundle, "
            f"{len(claims)} claims)\n\n" + "\n".join(ev_lines) +
            "\n\nProduce the strict-JSON output now.")


# Timeout policy. A FIXED budget was wrong in a way that punished doing the
# evidence work properly (measured 2026-09-04): the runbook's 420s came from
# a 64-claim bundle, and once bundles reached full ten-dimension coverage at
# 88-115 claims, epl2627_gw03_bri_lee's prompt (82,800 chars) needed 468s --
# 48 seconds over -- so all three samples were killed just short of finishing,
# twice, on a quiet machine. A better evidence bundle made J unrunnable, which
# is exactly backwards.
#
# So the budget is derived from prompt size. The rate is ~2x the one
# observation (468s / 82,800 chars = 5.65ms per char) because the sample-level
# variance is real and the costs are asymmetric: a sample killed 48s short
# wastes the entire 420s AND returns nothing, while a generous ceiling only
# costs time on a sample that was going to hang anyway. The floor keeps small
# prompts from getting an unreasonably tight budget.
SECONDS_PER_PROMPT_CHAR = 0.0113
MIN_TIMEOUT_S = 420


def suggested_timeout(prompt):
    """Seconds to allow one `claude -p` call for `prompt`. See the policy
    note above -- derived from prompt size, never a fixed constant."""
    return max(MIN_TIMEOUT_S, len(prompt or "") * SECONDS_PER_PROMPT_CHAR)


# Environment variables that make a child `claude` load THIS project's context
# (its CLAUDE.md) no matter which directory it runs in. Found 2026-10-06 in a
# Claude Code cloud sandbox: a `claude -p` started from the repo answered with
# an attempt to run CLAUDE.md's "first action" commands, and the sandbox sets
# CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD. J's whole design is "no tools,
# no session context, does not know the project", so the child must not inherit
# such a variable. Prefix match, because the exact names are not documented.
_CONTEXT_ENV_PREFIXES = ("CLAUDE_CODE_ADDITIONAL_DIRECTORIES",)


def isolated_env(environ=None):
    """A copy of the environment without the variables that inject project
    context into a child `claude`. Everything else (PATH, credentials) stays:
    the child still has to find the binary and log in."""
    env = dict(os.environ if environ is None else environ)
    for key in list(env):
        if key.startswith(_CONTEXT_ENV_PREFIXES):
            del env[key]
    return env


class LLMText(str):
    """The text of one model call plus WHICH model(s) wrote it.

    A `str` subclass on purpose: every consumer (parse_sample, json, regex)
    keeps working on a plain string, and a mock `llm` that returns a bare
    str simply reports no model -- recorded as unreported, never guessed.
    """
    models = ()          # authoring model(s), in the order they appeared
    model_usage = None   # {model: {inputTokens, outputTokens}} for EVERY
    #                      model the call touched, auxiliaries included


def _llm_text(text, models=(), model_usage=None):
    out = LLMText(text)
    out.models = tuple(models)
    out.model_usage = model_usage
    return out


def _parse_cli_stream(out):
    """Read the stdout of `claude -p --output-format stream-json --verbose`.

    Returns (text, models, model_usage), or None when `out` is not that
    format (a plain-text fatal such as an exhausted usage limit, or a
    stand-in process in a test) so the caller falls back to the raw text.

    WHY THE MODEL IS READ FROM THE ASSISTANT MESSAGES, NOT FROM
    `modelUsage`: the result event's `modelUsage` lists every model the call
    touched, auxiliaries included. Measured 2026-10-06 on a one-word reply:
    a Haiku auxiliary call logged 12 output tokens beside the authoring
    model's 5, so "the model with the most output" names the wrong one. The
    assistant messages carry the model that actually wrote the answer;
    `modelUsage` is kept only as usage."""
    events = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and "type" in e:
            events.append(e)
    result = next((e for e in reversed(events) if e.get("type") == "result"),
                  None)
    if result is None:
        return None
    models, texts = [], []
    for e in events:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message") or {}
        m = msg.get("model")
        # "<synthetic>" marks a message the CLI wrote itself (an API error
        # notice), not one a model authored
        if m and m != "<synthetic>" and m not in models:
            models.append(m)
        for c in msg.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text":
                texts.append(c.get("text") or "")
    if not models:
        init = next((e for e in events if e.get("type") == "system"
                     and e.get("subtype") == "init"), None)
        if init and init.get("model"):
            models.append(init["model"])
    text = result.get("result")
    if not isinstance(text, str):
        text = "".join(texts)
    usage = {m: {"inputTokens": u.get("inputTokens"),
                 "outputTokens": u.get("outputTokens")}
             for m, u in (result.get("modelUsage") or {}).items()
             if isinstance(u, dict)}
    return text, models, (usage or None)


def _default_argv(exe, model=None):
    argv = [exe, "-p", "--max-turns", "3", "--disallowed-tools", "*",
            "--output-format", "stream-json", "--verbose"]
    if model:
        argv += ["--model", model]
    return argv


def cli_llm(prompt, timeout=None, cwd=None, exe=None, argv=None, model=None):
    """claude -p subscription call.

    `model=None` leaves the choice to the CLI's default and RECORDS what ran
    (the returned text is an `LLMText`: `.models`, `.model_usage`); a value
    is passed as `--model`. Nothing pins the model: before 2026-10-06 the
    runner recorded only the spec version, so which model produced a J could
    not be recovered afterwards, and a model change could not be told apart
    from a spec change. Output is read as stream-json (see
    `_parse_cli_stream`); anything else falls back to the raw text, models
    unreported.

    THE TIMEOUT USED NOT TO BE A TIMEOUT (fixed 2026-08-26, found live on
    the first EPL J run: a k=3 batch nominally bounded at 3x180s was still
    running after 22 minutes). `subprocess.run(..., capture_output=True,
    timeout=T)` kills only the DIRECT child on expiry and then re-enters
    communicate() to drain the pipes. On Windows `claude` is installed as
    claude.CMD, so the direct child is cmd.exe and the real worker is a
    GRANDCHILD that inherited the stdout/stderr pipe handles: killing
    cmd.exe leaves it running, the pipe never reaches EOF, and the drain
    blocks forever. Confirmed by process tree at the time (python ->
    cmd.exe -> claude.exe, the claude.exe 12 minutes older than its own
    180s budget).

    Fixed two ways at once: stdio is redirected to FILES rather than pipes,
    so there is no pipe left to drain and no deadlock is possible; and on
    expiry the whole process TREE is killed (`_kill_process_tree`), not
    just the direct child. A hung sample now costs `timeout` seconds and is
    recorded as a reject, which is what run()'s per-sample error handling
    always assumed.

    `exe`/`argv` exist so the timeout path can be tested without invoking
    the real CLI (see `_selfcheck`); leave them None in production.

    Other Windows gotchas baked in: prompt via STDIN not argv (CJK argv
    mangles); encoding utf-8 (cp950 locale nulls output); NO --bare (skips
    subscription login); tools disabled (else it
    goes agentic and exhausts turns); executable resolved via shutil.which
    first — npm installs `claude` as claude.CMD/.ps1 on Windows, and passing
    the bare "claude" string to subprocess.run's list form skips PATHEXT
    resolution, raising FileNotFoundError (confirmed live in the first-ever
    run of this function from this package, 2026-07-17 smoke test — see
    reports/j_runner_trial_2026-07-17.md).

    `cwd` defaults to the system temp dir, NOT the caller's cwd — subprocess
    inherits the parent's cwd when none is given, and if that parent happens
    to be running from inside this project (which has its own CLAUDE.md
    discussing match IDs/locks by name), the "isolated" call is not actually
    isolated: --disallowed-tools "*" blocks the tool from EXECUTING, but does
    not stop the model from noticing it's sitting inside a repo that plausibly
    has the answer and attempting the call anyway. Confirmed live in the
    2026-07-17 retest (`reports/j_runner_retest_2026-07-17.md`): the identical
    prompt run from inside wc2026 attempted a blocked tool-use call to inspect
    the filesystem for match/team identity in 2 of 3 tries; from a neutral cwd,
    0 of 3. tempfile.gettempdir() has no .git/CLAUDE.md in its ancestry and
    needs no session-specific path, unlike the scratchpad directory used for
    the retest itself."""
    exe = exe or shutil.which("claude") or "claude"
    argv = list(argv) if argv is not None else _default_argv(exe, model)
    # `timeout=None` derives the budget from the prompt rather than
    # inheriting a constant somebody calibrated on a smaller bundle.
    if timeout is None:
        timeout = suggested_timeout(prompt)

    workdir = Path(tempfile.mkdtemp(prefix="j_runner_"))
    try:
        p_in = workdir / "prompt.txt"
        p_out = workdir / "stdout.txt"
        p_err = workdir / "stderr.txt"
        p_in.write_text(prompt, encoding="utf-8")
        with p_in.open("rb") as fin, p_out.open("wb") as fout, \
                p_err.open("wb") as ferr:
            # POSIX: the child must lead its OWN process group, or
            # _kill_process_tree's killpg() would signal our own group too
            # (the child inherits ours) -- on a timeout that kills this
            # runner and the shell above it, not just the hung `claude`.
            proc = subprocess.Popen(argv, stdin=fin, stdout=fout, stderr=ferr,
                                    cwd=cwd or tempfile.gettempdir(),
                                    env=isolated_env(),
                                    start_new_session=(os.name != "nt"))
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc.pid)
                # The post-kill reap can ITSELF expire when the tree is slow
                # to die, and that TimeoutExpired used to escape from inside
                # this handler -- so a sample that blew a 420s budget was
                # recorded as "timed out after 10 seconds" (seen live
                # 2026-09-03 on epl2627_gw03_new_bou). A reject line naming a
                # limit that does not exist sends the next reader looking for
                # the wrong bug, so the reap is contained and the message
                # states the real budget either way.
                try:
                    proc.wait(timeout=10)
                    reaped = "process tree killed"
                except subprocess.TimeoutExpired:
                    reaped = ("process tree kill issued but the tree had not "
                              "exited 10s later -- it may still be running")
                raise RuntimeError(
                    f"claude -p exceeded {timeout}s; {reaped}")
        out = p_out.read_text(encoding="utf-8", errors="replace")
        err = p_err.read_text(encoding="utf-8", errors="replace")
        if rc != 0:
            # `claude -p` writes some fatal conditions -- notably an
            # exhausted usage limit -- to STDOUT and leaves stderr empty, so
            # reporting stderr alone produced "failed rc=1: " with nothing
            # after the colon (seen 2026-09-04 across nine samples). A
            # non-zero exit with no diagnosis is indistinguishable from a
            # mystery, and the difference between "out of quota, retry after
            # the reset" and "something is broken" decides the next action.
            # with stream-json the same diagnosis sits in the result event;
            # report that text, not a screenful of event JSON
            streamed = _parse_cli_stream(out)
            shown = streamed[0].strip() if streamed and streamed[0].strip() \
                else out.strip()
            detail = err.strip() or shown or "(no output on either stream)"
            raise RuntimeError(
                f"claude -p failed rc={rc}{_rc_hint(rc)}: {detail[:400]}")
        streamed = _parse_cli_stream(out)
        if streamed is None:
            return _llm_text(out)
        text, models, usage = streamed
        return _llm_text(text, models, usage)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Windows NTSTATUS codes seen from `claude -p` in this project, as unsigned
# ints (Python reports the raw 32-bit value). A bare "rc=3221225794" tells a
# reader nothing; the difference between "the machine could not start the
# process" and "the model refused" decides whether to retry or investigate.
_WINDOWS_RC_HINTS = {
    3221225794: (" (0xC0000142 STATUS_DLL_INIT_FAILED -- the process could "
                 "not START; on this machine it appears under resource "
                 "pressure, e.g. many agents running at once. An "
                 "environment failure, not a model refusal: retry when the "
                 "machine is quieter rather than changing the prompt)"),
    3221225477: " (0xC0000005 access violation -- the CLI crashed)",
    3221225786: " (0xC000013A -- the process was interrupted, e.g. Ctrl-C)",
}


def _rc_hint(rc):
    """A human-readable tail for a non-zero exit code, or '' if unknown."""
    return _WINDOWS_RC_HINTS.get(rc, "")


def _kill_process_tree(pid):
    """Kill `pid` AND its descendants. On Windows `claude` is installed as
    claude.CMD, so the tree is python -> cmd.exe -> claude.exe -> node;
    killing only the direct child leaves the real worker alive (see
    cli_llm's docstring for what that cost)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True)
        return
    # POSIX path: needs cli_llm's start_new_session=True so the pgid below
    # is the child's own. Written 2026-07-17, first reachable on Linux
    # with the cloud move; no Linux machine has run it yet.
    try:  # pragma: no cover - POSIX path, not exercised on this machine
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# Keys every factor carries, whichever lever it pulls.
COMMON_FACTOR_KEYS = {"mechanism_type", "direction", "evidence_refs",
                      "absorption_case", "magnitude_note",
                      "materialisation_check"}
# Lever-specific required key. v1 knew only the strength lever, so a factor
# with no "lever" field is a d_eff factor by definition. v2 added the tempo
# lever, whose quantity is a goals multiplier -- demanding `d_eff_shift` of
# a tempo factor (as the old flat REQUIRED_FACTOR_KEYS did) would have
# rejected every spec-legal v2 tempo factor.
LEVER_REQUIRED_KEY = {"d_eff": "d_eff_shift", "tempo": "total_goals_multiplier"}
# rr1.md section on the tempo lever states this band. It was written in the
# spec but never enforced here until 2026-10-06, so a J that output a x3.0
# goals multiplier would have been accepted.
TEMPO_MULTIPLIER_RANGE = (0.75, 1.25)
REQUIRED_FACTOR_KEYS = COMMON_FACTOR_KEYS | {"d_eff_shift"}  # back-compat alias


def implied_probs_from_factors(m_payload, factors):
    """What this sample's OWN factors mechanically imply, recomputed here.

    WHY THIS EXISTS (2026-08-26). The whole system's stated doctrine is that
    the LLM supplies judgment and the machine supplies arithmetic -- and yet
    instrument J was doing its own arithmetic and nobody checked it. Spec v2
    onward requires probs_90 to be COMPUTED through the goal model (strength
    shifts -> lambdas from the calibrated curve -> tempo multiplier ->
    Dixon-Coles grid), but `parse_sample` only ever verified the degenerate
    case: empty factors must reproduce M. A sample with factors could state
    any probabilities at all and pass, so "J moved 5pp for these reasons"
    was an unverified claim about the model, not a reading of it.

    Returns a {"home","draw","away"} dict, or None when `m_payload` does not
    carry what the recomputation needs (`inputs.elo_diff_effective` and an
    `inputs.goal_curve` of g_a/g_b/dixon_coles_rho) -- an older payload
    without them is not a failure, it just cannot be checked, and that
    difference is reported rather than hidden.
    """
    from football_ai.models.m_dc import outcome_probs

    inputs = (m_payload or {}).get("inputs") or {}
    curve = inputs.get("goal_curve") or {}
    d_eff = inputs.get("elo_diff_effective")
    g_a, g_b = curve.get("g_a"), curve.get("g_b")
    rho = curve.get("dixon_coles_rho")
    if d_eff is None or g_a is None or g_b is None or rho is None:
        return None

    # d_eff_shift is already signed toward `direction` (spec v1: a factor
    # favouring the away side carries a negative shift), so factors sum.
    shift = 0.0
    tempo = 1.0
    for f in factors or []:
        lever = f.get("lever", "d_eff")
        if lever == "tempo":
            tempo *= float(f.get("total_goals_multiplier", 1.0))
        else:
            shift += float(f.get("d_eff_shift", 0.0))

    d = float(d_eff) + shift
    lam_h = math.exp(g_a + g_b * d / 100.0) * tempo
    lam_a = math.exp(g_a - g_b * d / 100.0) * tempo
    home, draw, away = outcome_probs(lam_h, lam_a, rho)
    return {"home": home, "draw": draw, "away": away}


# The mechanism types rr1.md lists. The ledger groups precedents by this
# string (`sizing.precedent_stats`), so a free-form label is not harmless: it
# splits one mechanism's record across names it will never pool again.
MECHANISM_TYPES_RR1 = frozenset({
    "squad_composition_change", "injury_replacement",
    "rotation_discount_recovery", "rest_travel",
    "tactical_stylistic_matchup", "incentive", "other"})


def parse_sample(text, m_probs, tol=1e-6, require_qualify=True,
                 m_payload=None, arith_tol_pp=3.0, n_claims=None,
                 mechanism_types=None):
    """Parse + validate one model output against the spec's mechanical
    rules. Returns the sample dict; raises ValueError with the reason.

    `require_qualify=False` for a round-robin competition, whose spec
    (j_spec/v2-rr.md) tells the instrument to omit `qualify_home` because
    the market does not exist there."""
    m = _JSON_RE.search(text or "")
    if not m:
        raise ValueError("no JSON object in output")
    s = json.loads(m.group(0))
    p = s.get("probs_90", {})
    vec = [p.get("home"), p.get("draw"), p.get("away")]
    if any(v is None or not (0.0 < v < 1.0) for v in vec):
        raise ValueError(f"probs_90 out of range: {vec}")
    if abs(sum(vec) - 1.0) > 1e-3:
        raise ValueError(f"probs_90 sums to {sum(vec):.4f}")
    if require_qualify:
        if not (0.0 < s.get("qualify_home", -1) < 1.0):
            raise ValueError("qualify_home out of range")
    elif "qualify_home" in s:
        raise ValueError(
            "qualify_home present for a competition that has no "
            "advancement market -- the instrument invented a number")
    factors = s.get("factors")
    if factors is None or not isinstance(factors, list):
        raise ValueError("factors list required (empty list means J=M)")
    for f in factors:
        lever = f.get("lever", "d_eff")
        if lever not in LEVER_REQUIRED_KEY:
            raise ValueError(f"unknown factor lever {lever!r}")
        needed = COMMON_FACTOR_KEYS | {LEVER_REQUIRED_KEY[lever]}
        missing = needed - set(f)
        if missing:
            raise ValueError(f"{lever} factor missing keys: {sorted(missing)}")
        if mechanism_types is not None and f.get("mechanism_type") not in mechanism_types:
            raise ValueError(
                f"unknown mechanism_type {f.get('mechanism_type')!r}; the spec "
                f"lists {sorted(mechanism_types)}")
        if n_claims is not None:
            refs = f.get("evidence_refs")
            if (not isinstance(refs, list) or not refs or any(
                    not isinstance(i, int) or isinstance(i, bool)
                    or not (0 <= i < n_claims) for i in refs)):
                raise ValueError(
                    f"evidence_refs must be a non-empty list of indices into the "
                    f"{n_claims}-claim bundle: a factor that cites evidence "
                    f"that is not there is a claim without a source")
        if lever == "tempo" and f.get("direction") != "none":
            raise ValueError(
                "a tempo factor must have direction 'none' (spec v2: it "
                "changes the whole score distribution without taking a side)")
        if lever == "tempo":
            lo, hi = TEMPO_MULTIPLIER_RANGE
            try:
                mult = float(f["total_goals_multiplier"])
            except (TypeError, ValueError):
                raise ValueError(
                    "tempo total_goals_multiplier must be a number") from None
            if not (lo <= mult <= hi):
                raise ValueError(
                    f"tempo total_goals_multiplier {mult} is outside the "
                    f"spec's [{lo}, {hi}] band")

    # Arithmetic gate: the stated probabilities must be the ones this
    # sample's own factors produce through the goal model. A generous
    # tolerance -- the point is to catch probabilities that were never
    # computed at all, not to police rounding.
    implied = implied_probs_from_factors(m_payload, factors)
    if implied is not None:
        delta_pp = max(abs(implied[k] - p[k]) * 100.0
                       for k in ("home", "draw", "away"))
        s["implied_probs_90"] = implied
        s["arithmetic_delta_pp"] = delta_pp
        if delta_pp > arith_tol_pp:
            raise ValueError(
                f"stated probs_90 are {delta_pp:.2f}pp from what this "
                f"sample's own factors imply through the goal model "
                f"(implied {implied['home']:.4f}/{implied['draw']:.4f}/"
                f"{implied['away']:.4f}) -- spec requires probs_90 to be "
                f"COMPUTED from the model, not asserted alongside it")
    if not factors:
        mv = [m_probs["home"], m_probs["draw"], m_probs["away"]]
        if any(abs(a - b) > tol for a, b in zip(vec, mv)):
            raise ValueError(
                "spec violation: empty factors but probs_90 != M — "
                "deviation without a nameable mechanism is forbidden")
    if not s.get("basis"):
        raise ValueError("basis required")
    return s


def aggregate(samples):
    """Median point estimate + dispersion + a representative sample (the
    one closest to the aggregate, whose factors/basis are carried up)."""
    keys = ["home", "draw", "away"]
    med = {k: statistics.median(s["probs_90"][k] for s in samples)
           for k in keys}
    tot = sum(med.values())
    med = {k: v / tot for k, v in med.items()}
    dispersion = max(
        max(s["probs_90"][k] for s in samples)
        - min(s["probs_90"][k] for s in samples)
        for k in keys)
    quals = [s["qualify_home"] for s in samples if "qualify_home" in s]
    qualify = statistics.median(quals) if quals else None

    def dist(s):
        return sum(abs(s["probs_90"][k] - med[k]) for k in keys)

    rep = min(samples, key=dist)
    return med, qualify, dispersion, rep


def run(m_payload, claims, llm, k=3, spec_version=CURRENT_SPEC,
        require_qualify=None, allow_archived_spec=False,
        model_requested=None, parallel=True):
    """Produce the lock-ready "j" payload. `llm` is the text->text callable
    (cli_llm for real runs; a mock in tests). Raises if every sample is
    invalid — a broken instrument must fail loudly, never lock garbage.

    A single sample's llm() call itself failing (timeout, non-zero exit via
    cli_llm's RuntimeError, etc.) is caught and recorded as a reject rather
    than propagating — previously an uncaught exception from llm() crashed
    the whole batch and discarded any already-successful samples in the same
    k=3 run, which contradicts this function's own doctrine ("k samples...
    the spread is reported as honest uncertainty" / "Raises if every sample
    is invalid" implies partial failure among k is expected and tolerated,
    not fatal). Confirmed live 2026-07-17 smoke test: one cli_llm() call hit
    its 180s timeout and an uncaught subprocess.TimeoutExpired discarded the
    whole run — see reports/j_runner_trial_2026-07-17.md."""
    # `require_qualify=None` derives it from M itself: a payload with no
    # advancement probability is a competition with no advancement market.
    if require_qualify is None:
        require_qualify = "qualify_home" in m_payload
    version, spec_text = load_spec(spec_version,
                                   allow_archived=allow_archived_spec)
    prompt = assemble_prompt(m_payload, claims, spec_text)
    samples, rejects = [], []

    def one(i):
        try:
            out = llm(prompt)
        except Exception as e:
            return None, f"sample {i}: llm call failed: {e}"
        try:
            smp = parse_sample(out, m_payload["probs_90"],
                               require_qualify=require_qualify,
                               m_payload=m_payload, n_claims=len(claims),
                               mechanism_types=(MECHANISM_TYPES_RR1
                                                if version == "rr1" else None))
        except (ValueError, json.JSONDecodeError) as e:
            return None, f"sample {i}: {e}"
        # which model wrote THIS sample; [] = the callable did not say
        smp["models"] = list(getattr(out, "models", None) or ())
        usage = getattr(out, "model_usage", None)
        if usage:
            smp["model_usage"] = usage
        return smp, None

    # The k samples are independent subprocesses, so they run side by side: a
    # J took k times as long as one sample (10+ minutes each) against a
    # deadline measured in hours for a whole matchweek. `map` keeps index
    # order, so reject messages and the sample list read the same as before.
    if parallel and k > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=k) as pool:
            results = list(pool.map(one, range(k)))
    else:
        results = [one(i) for i in range(k)]
    for smp, reject in results:
        if smp is not None:
            samples.append(smp)
        else:
            rejects.append(reject)
    if not samples:
        raise RuntimeError(f"all {k} samples invalid: {rejects}")
    models_seen = sorted({m for s in samples for m in s["models"]})
    med, qualify, dispersion, rep = aggregate(samples)
    out_j = {
        "probs_90": med,
        "basis": rep["basis"],
        "factors": rep["factors"],
        "invalidation": rep.get("invalidation", []),
        "self_flags": rep.get("self_flags", []),
        "runner": {
            "j_spec_version": version,
            # who produced this J: the instrument is spec x bundle x M x
            # MODEL, and only the first three used to be recorded.
            # `model_requested` None = the CLI's default was used; `models`
            # is what actually answered ([] = not reported, see model_note).
            "models": models_seen,
            "model_requested": model_requested,
            **({} if models_seen else {
                "model_note": "the llm callable reported no model (a mock, "
                              "or CLI output that was not stream-json); "
                              "unrecorded, not unknown-by-default"}),
            "k_requested": k,
            "k_valid": len(samples),
            "rejects": rejects,
            "dispersion": dispersion,
            "samples": samples,
            "note": ("median-of-k isolated runs; dispersion is honest "
                     "uncertainty — high dispersion means J has no stable "
                     "view. The invoking session's context never reaches "
                     "the model."),
        },
    }

    # Arithmetic audit: how far each sample's stated probabilities sat from
    # what its own factors imply, plus the median of the MECHANICALLY
    # recomputed probabilities. The point estimate above stays the median of
    # STATED probs (the pre-registered contract), but a consumer that would
    # rather trust the machine's arithmetic than the model's now has the
    # number instead of having to take it on faith.
    deltas = [s["arithmetic_delta_pp"] for s in samples
              if "arithmetic_delta_pp" in s]
    if deltas:
        implied_med = {
            k: statistics.median(s["implied_probs_90"][k] for s in samples
                                 if "implied_probs_90" in s)
            for k in ("home", "draw", "away")}
        tot_i = sum(implied_med.values())
        out_j["runner"]["arithmetic"] = {
            "max_delta_pp": max(deltas),
            "per_sample_delta_pp": deltas,
            "implied_median_probs_90": {k: v / tot_i
                                        for k, v in implied_med.items()},
            "note": ("stated probs vs the goal model recomputed here from "
                     "each sample's own factors; the LLM does the judgment, "
                     "this checks the arithmetic it was asked not to be "
                     "trusted with"),
        }
    else:
        out_j["runner"]["arithmetic"] = {
            "checked": False,
            "note": ("m_payload carried no inputs.goal_curve / "
                     "inputs.elo_diff_effective, so J's arithmetic could "
                     "not be verified — not a failure, but not a check"),
        }
    if qualify is not None:
        out_j["qualify_home"] = qualify
    return out_j


def _selfcheck():
    fails = []
    # J's subprocess must not inherit a variable that injects project context
    _env = isolated_env({"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
                         "CLAUDE_CODE_ADDITIONAL_DIRECTORIES": "/x",
                         "PATH": "/bin", "HOME": "/h"})
    if any(k.startswith("CLAUDE_CODE_ADDITIONAL_DIRECTORIES") for k in _env):
        fails.append("isolated_env must drop the variables that load the "
                     "project's CLAUDE.md into a child claude")
    if _env.get("PATH") != "/bin" or _env.get("HOME") != "/h":
        fails.append("isolated_env must keep PATH and HOME (the child must "
                     "still find the binary and its login)")
    m_payload = {
        "probs_90": {"home": 0.5, "draw": 0.3, "away": 0.2},
        "qualify_home": 0.6,
        "lambdas": {"home": 1.4, "away": 1.0},
    }
    claims = [{"claim": "starter X out", "category": "injury",
               "source_tier": "1b", "source": "ESPN",
               "access_time": "2026-07-08T00:00:00+00:00",
               "absorption": "likely_unpriced",
               "absorption_reason": "broke 30 minutes ago"}]

    def mk(home, factors):
        draw = 0.3
        return json.dumps({
            "probs_90": {"home": home, "draw": draw,
                         "away": 1.0 - home - draw},
            "qualify_home": 0.6 + (home - 0.5),
            "factors": factors, "basis": ["test basis"],
            "invalidation": ["lineup flips"], "self_flags": [],
        })

    factor = {"mechanism_type": "injury_replacement", "direction": "away",
              "d_eff_shift": -20, "evidence_refs": [0],
              "absorption_case": "fresh news", "magnitude_note": "half",
              "materialisation_check": "did the replacement start"}
    outs = iter([mk(0.46, [factor]), mk(0.48, [factor]),
                 mk(0.44, [factor])])
    j = run(m_payload, claims, llm=lambda p: next(outs), k=3)
    if abs(sum(j["probs_90"].values()) - 1.0) > 1e-9:
        fails.append("aggregate probs must renormalize to 1")
    if abs(j["probs_90"]["home"] - 0.46 / (0.46 + 0.3 + 0.24)) > 1e-9:
        fails.append("median-of-k point estimate wrong")
    if j["runner"]["k_valid"] != 3 or j["runner"]["dispersion"] <= 0:
        fails.append("runner metadata wrong")
    # samples run in parallel: three 0.6s samples take about 0.6s, not 1.8s,
    # and the sequential path still works and is the slow one
    import time as _t
    def _slow(p):
        _t.sleep(0.6)
        return mk(0.46, [factor])
    t0 = _t.monotonic()
    run(m_payload, claims, llm=_slow, k=3)
    par_s = _t.monotonic() - t0
    t0 = _t.monotonic()
    run(m_payload, claims, llm=_slow, k=3, parallel=False)
    seq_s = _t.monotonic() - t0
    if par_s > 1.3 or seq_s < 1.7:
        fails.append(f"k samples must run in parallel by default: parallel "
                     f"{par_s:.1f}s (want ~0.6), sequential {seq_s:.1f}s (want ~1.8)")
    # reject messages keep their sample index under parallel execution
    calls_ = iter([mk(0.46, [factor]), "garbage", mk(0.48, [factor])])
    jr = run(m_payload, claims, llm=lambda p: next(calls_), k=3)
    if jr["runner"]["k_valid"] != 2 or len(jr["runner"]["rejects"]) != 1 \
            or not jr["runner"]["rejects"][0].startswith("sample "):
        fails.append(f"one bad sample of three must leave k_valid=2 and one "
                     f"indexed reject: {jr['runner']['rejects']}")
    # spec compliance the arithmetic gate cannot see: a factor must cite
    # evidence that exists, and its mechanism type must be one the spec lists
    ok_json = mk(0.46, [factor])
    for label, text, kw in (
            ("a dangling evidence ref", mk(0.46, [dict(factor, evidence_refs=[99])]), {"n_claims": len(claims)}),
            ("an empty evidence_refs", mk(0.46, [dict(factor, evidence_refs=[])]), {"n_claims": len(claims)}),
            ("a boolean as an evidence ref", mk(0.46, [dict(factor, evidence_refs=[True])]), {"n_claims": len(claims)}),
            ("a mechanism type outside the spec", mk(0.46, [dict(factor, mechanism_type="vibes")]),
             {"mechanism_types": MECHANISM_TYPES_RR1})):
        try:
            parse_sample(text, m_payload["probs_90"], **kw)
            fails.append(f"parse_sample must reject {label}")
        except ValueError:
            pass
    try:
        parse_sample(ok_json, m_payload["probs_90"], n_claims=len(claims),
                     mechanism_types=MECHANISM_TYPES_RR1)
    except ValueError as e:
        fails.append(f"a compliant sample must still pass the stricter checks: {e}")
    # a callable that names no model is recorded as UNREPORTED, loudly
    if j["runner"]["models"] != [] or "model_note" not in j["runner"] \
            or j["runner"]["model_requested"] is not None:
        fails.append("an llm that reports no model must be recorded as "
                     "unreported (models=[] plus model_note), not guessed")
    # one that does name it is recorded per sample and for the run
    usage_ = {"m-main": {"inputTokens": 2, "outputTokens": 5}}
    outs2 = iter([_llm_text(mk(0.46, [factor]), ["m-main"], usage_),
                  _llm_text(mk(0.48, [factor]), ["m-main"], usage_)])
    j2 = run(m_payload, claims, llm=lambda p: next(outs2), k=2,
             model_requested="m-main")
    if j2["runner"]["models"] != ["m-main"] \
            or j2["runner"]["model_requested"] != "m-main" \
            or "model_note" in j2["runner"] \
            or any(s.get("models") != ["m-main"]
                   for s in j2["runner"]["samples"]):
        fails.append("a reported model must be recorded on every sample and "
                     "on the runner block")
    # spec rule enforced mechanically: no factors but probs != M
    bad = json.dumps({"probs_90": {"home": 0.6, "draw": 0.25, "away": 0.15},
                      "qualify_home": 0.7, "factors": [],
                      "basis": ["moved with no mechanism"]})
    try:
        parse_sample(bad, m_payload["probs_90"])
        fails.append("deviation without factors must be rejected")
    except ValueError:
        pass
    # J=M with empty factors is legal
    eq = json.dumps({"probs_90": m_payload["probs_90"],
                     "qualify_home": 0.6, "factors": [],
                     "basis": ["nothing the market lacks"]})
    try:
        parse_sample(eq, m_payload["probs_90"])
    except ValueError:
        fails.append("J=M with empty factors must be accepted")
    # all-invalid must raise
    try:
        run(m_payload, claims, llm=lambda p: "not json", k=2)
        fails.append("all-invalid samples must raise")
    except RuntimeError:
        pass

    # --- arithmetic gate: J's stated probs must be what its own factors
    # produce through the goal model. Real EPL 2026-27 curve, so the
    # numbers below are the ones the live instrument actually works with.
    curve_payload = {
        "teams": {"home": "H", "away": "A"},
        "probs_90": {"home": 0.3196278263059043,
                     "draw": 0.26591781047712415,
                     "away": 0.4144543632169717},
        "lambdas": {"home": 1.2446103586719063, "away": 1.4505616969017707},
        "inputs": {"elo_diff_effective": -46.5601,
                   "goal_curve": {"g_a": 0.295387, "g_b": 0.164443,
                                  "dixon_coles_rho": -0.037163}},
    }
    shift12 = {"mechanism_type": "other", "lever": "d_eff",
               "d_eff_shift": 12, "direction": "home", "evidence_refs": [0],
               "absorption_case": "x", "magnitude_note": "y",
               "materialisation_check": "z"}
    implied12 = implied_probs_from_factors(curve_payload, [shift12])
    if implied12 is None:
        fails.append("implied_probs_from_factors returned None on a payload "
                     "that carries a full goal_curve")
    else:
        # a +12 shift on d_eff -46.56 must move the home side UP, and the
        # recomputation must reproduce the locked M when nothing is shifted
        base = implied_probs_from_factors(curve_payload, [])
        if abs(base["home"] - curve_payload["probs_90"]["home"]) > 1e-6:
            fails.append(f"zero-factor recomputation must reproduce M, got "
                         f"{base['home']:.6f}")
        if implied12["home"] <= base["home"]:
            fails.append("a positive d_eff_shift toward home must raise the "
                         "home probability")

        def sample_json(probs, factors):
            return json.dumps({"probs_90": probs, "factors": factors,
                               "basis": ["b"], "invalidation": [],
                               "self_flags": []})

        honest = sample_json({k: round(v, 4) for k, v in implied12.items()},
                             [shift12])
        s_ok = parse_sample(honest, curve_payload["probs_90"],
                            require_qualify=False, m_payload=curve_payload)
        if s_ok.get("arithmetic_delta_pp", 99) > 0.05:
            fails.append(f"an honestly computed sample should show ~0 "
                         f"arithmetic delta, got {s_ok.get('arithmetic_delta_pp')}")
        if "implied_probs_90" not in s_ok:
            fails.append("a checked sample must carry implied_probs_90")

        # probabilities that were asserted rather than computed
        liar = sample_json({"home": 0.42, "draw": 0.26, "away": 0.32},
                           [shift12])
        try:
            parse_sample(liar, curve_payload["probs_90"],
                         require_qualify=False, m_payload=curve_payload)
            fails.append("probs_90 far from what the sample's own factors "
                         "imply must be rejected — otherwise J can assert "
                         "any number and call it a model output")
        except ValueError:
            pass

        # a tempo factor must move both lambdas and therefore the draw
        tempo_f = {"mechanism_type": "tactical_stylistic_matchup",
                   "lever": "tempo", "total_goals_multiplier": 0.85,
                   "direction": "none", "evidence_refs": [0],
                   "absorption_case": "x", "magnitude_note": "y"}
        quiet = implied_probs_from_factors(curve_payload, [tempo_f])
        if quiet["draw"] <= base["draw"]:
            fails.append("a tempo multiplier below 1 must fatten the draw")

    # an M payload with no curve cannot be checked, and must say so rather
    # than silently passing everything
    nocurve = {"probs_90": {"home": 0.4, "draw": 0.27, "away": 0.33},
               "lambdas": {"home": 1.3, "away": 1.2}}
    if implied_probs_from_factors(nocurve, [shift12]) is not None:
        fails.append("a payload without goal_curve must return None, not a "
                     "silently wrong recomputation")

    # --- cli_llm's timeout must actually bound the call, including when
    # the child spawns its own grandchild that outlives it (the 2026-08-26
    # 22-minute hang). Uses a stand-in process, never the real CLI.
    import subprocess as _sp
    import sys as _sys
    import time as _time

    grandchild_script = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
        "stdout=sys.stdout,stderr=sys.stderr);"
        "time.sleep(60)")
    t0 = _time.monotonic()
    try:
        cli_llm("x", timeout=3, argv=[_sys.executable, "-c", grandchild_script])
        fails.append("cli_llm must raise when the child outlives its timeout")
    except RuntimeError as e:
        if "kill" not in str(e):
            fails.append(f"timeout should report the tree kill, got: {e}")
        # The message must name the REAL budget. Live 2026-09-03 on
        # epl2627_gw03_new_bou, a sample that blew its 420s budget was
        # recorded as "timed out after 10 seconds", because the post-kill
        # reap's own TimeoutExpired escaped from inside this handler. A
        # reject line citing a limit that does not exist sends the next
        # reader hunting the wrong bug.
        if "3s" not in str(e):
            fails.append(f"the timeout message must state the budget that "
                         f"was actually exceeded (3s here), got: {e}")
        if "10 second" in str(e):
            fails.append("the post-kill reap's own 10s wait must never be "
                         "reported as the sample's timeout")
    except _sp.TimeoutExpired:
        fails.append("cli_llm leaked a raw TimeoutExpired instead of "
                     "reporting a killed tree")
    elapsed = _time.monotonic() - t0
    if elapsed > 30:
        fails.append(f"cli_llm timeout did not bound the call: {elapsed:.0f}s "
                     f"for a 3s budget (the pipe-drain deadlock is back)")

    # and a normal call still returns the child's stdout
    out = cli_llm("ignored", timeout=30,
                  argv=[_sys.executable, "-c", "print('hello-from-child')"])
    if "hello-from-child" not in out:
        fails.append(f"cli_llm did not return the child's stdout: {out!r}")
    if getattr(out, "models", None) != ():
        fails.append("plain-text child output must report no model")

    # --- which model answered (2026-10-06) --------------------------------
    stream = "\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init", "model": "m-main"},
        {"type": "assistant", "message": {
            "model": "m-main",
            "content": [{"type": "text", "text": "PONG"}]}},
        {"type": "result", "subtype": "success", "result": "PONG",
         "modelUsage": {"m-aux": {"inputTokens": 900, "outputTokens": 12},
                        "m-main": {"inputTokens": 2, "outputTokens": 5}}},
    ])
    parsed = _parse_cli_stream(stream)
    if parsed is None or parsed[0] != "PONG" or parsed[1] != ["m-main"]:
        fails.append(f"stream parse must name the AUTHORING model, got {parsed!r}")
    elif set(parsed[2]) != {"m-aux", "m-main"}:
        fails.append("modelUsage must keep every model the call touched")
    # the auxiliary had more output tokens; picking by volume would be wrong
    # (this is the exact shape measured live on 2026-10-06)
    if parsed and "m-aux" in parsed[1]:
        fails.append("an auxiliary model must never be reported as the author")
    if _parse_cli_stream("LIMIT REACHED\nplain text\n{not json") is not None:
        fails.append("plain text must fall back to the raw-text path")
    # a JSON ANSWER in text mode is not a stream event
    if _parse_cli_stream('{"probs_90": {"home": 0.5}}') is not None:
        fails.append("a bare JSON answer must not be mistaken for a stream")
    trunc = "\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init", "model": "m-init"},
        {"type": "assistant", "message": {
            "model": "<synthetic>",
            "content": [{"type": "text", "text": "API error notice"}]}},
        {"type": "result", "subtype": "error_max_turns"},
    ])
    parsed_t = _parse_cli_stream(trunc)
    if parsed_t is None or parsed_t[0] != "API error notice" \
            or parsed_t[1] != ["m-init"]:
        fails.append(f"no result text -> assistant text; synthetic is not an "
                     f"author, init model is the fallback; got {parsed_t!r}")
    # end to end through a real subprocess
    out_s = cli_llm("ignored", timeout=30, argv=[
        _sys.executable, "-c", f"print({stream!r})"])
    if str(out_s) != "PONG" or out_s.models != ("m-main",) \
            or not out_s.model_usage:
        fails.append(f"cli_llm must return the text with its models "
                     f"attached, got {out_s!r} {out_s.models!r}")
    try:
        cli_llm("x", timeout=30, argv=[
            _sys.executable, "-c",
            "import json,sys;"
            "print(json.dumps({'type':'result','subtype':'success',"
            "'is_error':True,'result':'USAGE-LIMIT-IN-RESULT'}));sys.exit(1)"])
        fails.append("a non-zero exit must raise")
    except RuntimeError as e:
        if "USAGE-LIMIT-IN-RESULT" not in str(e) or '"type"' in str(e):
            fails.append(f"a stream-format failure must report the result "
                         f"text, not event JSON, got: {e}")
    # the isolation flags must survive any change to the output format, and
    # --model appears only when asked for
    base_argv = _default_argv("claude")
    if "--disallowed-tools" not in base_argv or "*" not in base_argv \
            or "--model" in base_argv or "stream-json" not in base_argv:
        fails.append(f"default argv lost isolation/format flags: {base_argv}")
    if _default_argv("claude", "m-x")[-2:] != ["--model", "m-x"]:
        fails.append("model= must be passed as --model")

    # a non-zero exit whose diagnosis went to STDOUT must still be reported:
    # an exhausted usage limit surfaces exactly this way, and nine samples
    # were once recorded as "failed rc=1: " with nothing after the colon
    try:
        cli_llm("x", timeout=30, argv=[
            _sys.executable, "-c",
            "import sys;print('LIMIT-REACHED-ON-STDOUT');sys.exit(1)"])
        fails.append("a non-zero exit must raise")
    except RuntimeError as e:
        if "LIMIT-REACHED-ON-STDOUT" not in str(e):
            fails.append(f"a failure diagnosed only on stdout must still be "
                         f"reported, got: {e}")

    # --- the timeout must SCALE with the prompt (2026-09-04) --------------
    # A fixed budget punished better evidence: bri_lee's 82,800-char prompt
    # needed 468s against the runbook's 420s, so all three samples were
    # killed 48 seconds short, twice, on an idle machine.
    if suggested_timeout("x" * 82_800) < 468:
        fails.append("the derived budget must exceed the 468s actually "
                     "measured for an 82,800-char prompt, or the failure "
                     "that motivated this policy recurs")
    if suggested_timeout("") != MIN_TIMEOUT_S:
        fails.append("a tiny prompt should still get the floor budget")
    if not (suggested_timeout("x" * 108_470)
            > suggested_timeout("x" * 82_800)):
        fails.append("a larger prompt must get a larger budget")
    # and the derivation must actually be reachable through cli_llm's
    # default -- a policy nothing calls is not a policy
    import inspect as _inspect
    if _inspect.signature(cli_llm).parameters["timeout"].default is not None:
        fails.append("cli_llm's timeout must default to None so the budget "
                     "is derived, not inherited from a stale constant")

    # --- spec surface: exactly one callable spec, archives refused -----
    ver, spec_text = load_spec()
    if ver != "rr1":
        fails.append(f"the default spec must be rr1, got {ver!r}")
    if "qualify" in spec_text.lower() and "不要輸出" not in spec_text:
        fails.append("the round-robin spec must not ask for an advancement "
                     "probability except to forbid it")
    if "---\n\n# The following supersedes" in spec_text:
        fails.append("rr1 must be self-contained, not composed")
    for archived in ("v1", "v2", "v2-rr"):
        try:
            load_spec(archived)
            fails.append(f"{archived!r} is archived and must be refused for "
                         f"new work — leaving it callable is how a session "
                         f"picks up a spec it merely found lying around")
        except ValueError:
            pass
        try:
            load_spec(archived, allow_archived=True)
        except ValueError:
            fails.append(f"{archived!r} must still load for reproducing an "
                         f"old lock that recorded it")
    try:
        load_spec("v99")
        fails.append("an unknown spec version must raise")
    except ValueError:
        pass

    # --- round-robin path: no advancement market anywhere ---
    rr_payload = {
        "teams": {"home": "H", "away": "A"},
        "probs_90": {"home": 0.4, "draw": 0.27, "away": 0.33},
        "lambdas": {"home": 1.3, "away": 1.2},
    }
    prompt = assemble_prompt(rr_payload, claims, "SPEC")
    if "qualify_home" in prompt:
        fails.append("round-robin prompt must not mention qualify_home")

    def rr_out(home, factors, extra=None):
        draw = 0.27
        body = {"probs_90": {"home": home, "draw": draw,
                             "away": round(1.0 - home - draw, 10)},
                "factors": factors, "basis": ["rr basis"],
                "invalidation": [], "self_flags": []}
        body.update(extra or {})
        return json.dumps(body)

    tempo = {"mechanism_type": "tactical_stylistic_matchup", "lever": "tempo",
             "total_goals_multiplier": 0.9, "direction": "none",
             "evidence_refs": [0], "absorption_case": "confirmed low block",
             "magnitude_note": "conservative",
             "materialisation_check": "shots conceded in open play"}
    outs = iter([rr_out(0.38, [tempo]), rr_out(0.40, [tempo]),
                 rr_out(0.39, [tempo])])
    jrr = run(rr_payload, claims, llm=lambda p: next(outs), k=3,
              spec_version="rr1")
    if "qualify_home" in jrr:
        fails.append("round-robin J payload must not carry qualify_home")
    if jrr["runner"]["k_valid"] != 3:
        fails.append(f"tempo-lever factors must validate, got "
                     f"{jrr['runner']['rejects']}")

    # inventing an advancement probability is a rejection, not a nicety
    try:
        parse_sample(rr_out(0.38, [tempo], {"qualify_home": 0.55}),
                     rr_payload["probs_90"], require_qualify=False)
        fails.append("a round-robin sample emitting qualify_home must be rejected")
    except ValueError:
        pass
    # ...and a knockout sample omitting it is still rejected
    try:
        parse_sample(rr_out(0.38, [tempo]), rr_payload["probs_90"],
                     require_qualify=True)
        fails.append("a knockout sample without qualify_home must be rejected")
    except ValueError:
        pass

    # --- lever-aware factor validation ---
    sided_tempo = dict(tempo, direction="home")
    try:
        parse_sample(rr_out(0.38, [sided_tempo]), rr_payload["probs_90"],
                     require_qualify=False)
        fails.append("a tempo factor taking a side must be rejected")
    except ValueError:
        pass
    nocheck = {k: v for k, v in factor.items() if k != "materialisation_check"}
    try:
        parse_sample(rr_out(0.38, [nocheck]), rr_payload["probs_90"],
                     require_qualify=False)
        fails.append("a factor with no materialisation_check must be rejected "
                     "— deciding after the match what counts as materialised "
                     "is hindsight, not a check")
    except ValueError:
        pass
    bare = {k: v for k, v in factor.items() if k != "d_eff_shift"}
    try:
        parse_sample(rr_out(0.38, [bare]), rr_payload["probs_90"],
                     require_qualify=False)
        fails.append("a d_eff factor without d_eff_shift must be rejected")
    except ValueError:
        pass
    # the spec's tempo band must actually be enforced
    wild = {k: v for k, v in factor.items() if k != "d_eff_shift"}
    wild.update(lever="tempo", direction="none", total_goals_multiplier=3.0)
    try:
        parse_sample(rr_out(0.38, [wild]), rr_payload["probs_90"],
                     require_qualify=False)
        fails.append("a tempo multiplier of 3.0 must be rejected: the spec "
                     "allows only [0.75, 1.25]")
    except ValueError as e:
        if "0.75" not in str(e):
            fails.append(f"a wild tempo multiplier was rejected for the wrong "
                         f"reason: {e}")

    return fails
