"""One-page decision summary of a matchweek, read from the ledger.

The page answers three questions in this order: may I bet and on what, what
must happen next and by when, and how do the layers compare on each fixture.
Detail sits behind the fixture's own row.

A fixture with no conclusion yet is never shown as "no bet": pending and
declined are different states and the page keeps them apart. Every number comes
from the ledger or the decision report; the page types none, computes only the
differences it displays, and writes nothing to the store. A judgment record
that contradicts itself (a stake on a fixture whose mechanism grade or gap does
not license one) is flagged on the page, not corrected.
"""
import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from football_ai.competitions import matchflow, schedule
from football_ai.decision import judgment, sizing
from football_ai.decision import report as decision_report
from football_ai.store import io as store_io

OUTCOMES = judgment.OUTCOMES
OUTCOME_LABEL = {"home": "主", "draw": "和", "away": "客"}
PRICED_PHASES = ("PRICED", "JUDGED", "J_FORECLOSED", "KICKED_OFF", "SETTLED")
STAKE_GRADES = ("verified", "graded")

PHASE_LABEL = {
    "UNLOCKED": "待鎖定", "M_LOCKED": "待 J", "J_DEGRADED": "J 須重跑",
    "J_LOCKED": "待價格", "J_FORECLOSED": "J 已失效", "PRICED": "待研判",
    "JUDGED": "已研判", "KICKED_OFF": "已開球", "SETTLED": "已結算",
}


# --- collecting -------------------------------------------------------------

def _ledger(config, kind):
    out = {}
    for r in store_io.read(kind, era=config.era):
        mid = r.get("match_id") or (r.get("data") or {}).get("match_id")
        out.setdefault(mid, []).append(r)
    return out


def _lock_view(rows):
    """M, the last J, its valid-sample count and the contamination flag, from
    one fixture's lock versions."""
    versions = sorted(rows, key=lambda r: r["data"].get("version", r.get("version", 1)))
    payloads = [(r["data"].get("payload") or {}) for r in versions]
    base = payloads[0]
    j = next((p["j"] for p in reversed(payloads) if p.get("j")), None)
    return {"m": base.get("probs_90"), "kickoff": base.get("kickoff_utc"),
            "j": (j or {}).get("probs_90"),
            "k_valid": ((j or {}).get("runner") or {}).get("k_valid"),
            "tainted": any(r["data"].get("contaminated_post_odds") for r in versions)
                       or any(p.get("contaminated_post_odds") for p in payloads)}


def _market_view(rep):
    blk = next((m for m in rep.get("markets") or [] if m.get("market") == "1x2"), None)
    if blk is None:
        return None
    return {"fair": blk["market_fair"], "odds": blk["odds"], "book": blk["best_book"],
            "at": blk.get("at"), "closing": bool(blk.get("closing")),
            "n_books": blk.get("n_books", 1)}


def _earliest_price(now, kickoff):
    """For a fixture with a usable J and no price: the first scheduled tick at
    which its entry price falls due (ops.fetch_due: kickoff within
    ENTRY_HORIZON_HOURS), or None if no tick comes before kickoff."""
    t = schedule.next_tick(max(now, kickoff - timedelta(hours=schedule.ENTRY_HORIZON_HOURS)))
    return t if t < kickoff else None


def _flags(config, row, rec, lock, mkt):
    flags = []
    graded = rec is not None and rec.get("mechanism_grade") in STAKE_GRADES
    staked = row["conclusion"] == "BET"
    if staked and not graded:
        flags.append(f"有注額,但機制等級為 {rec.get('mechanism_grade')!r}(須為 verified 或 graded)")
    if staked and row["gap_pp"] is not None and row["gap_pp"] < config.gate.bet_pp:
        flags.append(f"有注額,但 J 與市場的差距 {row['gap_pp']:.1f}pp 低於下注門檻 {config.gate.bet_pp:g}pp")
    if staked and not (rec.get("entry_price") or {}).get("decimal_odds"):
        flags.append("有注額,但記錄沒有進場價")
    if row["phase"] == "J_FORECLOSED" or (rec and rec.get("j_status") in ("foreclosed", "absent")):
        flags.append("沒有可比對的 J(永久缺口,非隨機缺失)")
    if lock and lock["tainted"]:
        flags.append("J 在價格出現後才寫入(contaminated_post_odds),不是獨立估計")
    if lock and lock["k_valid"] is not None:
        from football_ai.competitions.ops import MIN_K_FOR_PRICING
        if lock["k_valid"] < MIN_K_FOR_PRICING:
            flags.append(f"J 有效樣本只有 {lock['k_valid']}")
    if rec and rec.get("coverage_waiver"):
        flags.append(f"證據覆蓋不全,免責理由:{rec['coverage_waiver']}")
    if rec and "direction" in rec and rec.get("model") in (None, "undeclared"):
        flags.append("研判未申報模型")
    if row["conclusion"] == "PENDING" and row["phase"] in ("KICKED_OFF", "SETTLED"):
        flags.append("開球後仍無研判記錄(永久缺口)")
    if row["conclusion"] == "LEGACY":
        flags.append("既有記錄為舊格式,沒有方向與機率")
    if row["report_error"]:
        flags.append(f"報告無法產生:{row['report_error']}")
    return flags


def _missed(row):
    """No judgment and none can ever be written: kickoff has passed."""
    return row["phase"] in ("KICKED_OFF", "SETTLED") or bool(row["lost"])


def collect(comp, gameweek, now=None, build_fn=None, ledger_note_fn=None):
    """The data behind the page, one row per fixture of the matchweek."""
    config = comp["config"]
    now = now or datetime.now(timezone.utc)
    build_fn = build_fn or (lambda mid: decision_report.build(mid, mechanism=None, config=config))
    note_fn = matchflow.ledger_note if ledger_note_fn is None else ledger_note_fn
    fixtures = [f for f in comp.get("upcoming_fixtures") or [] if f.get("gameweek") == gameweek]
    flow = {r["match_id"]: r for r in matchflow.matchweek(config, fixtures, now=now)}
    locks = {mid: _lock_view(rs) for mid, rs in _ledger(config, "locks").items() if mid in flow}
    recs = {mid: rs[-1]["data"] for mid, rs in _ledger(config, "judgments").items() if mid in flow}

    rows = []
    for f in fixtures:
        mid = f["match_id"]
        fl, lock, rec = flow[mid], locks.get(mid), recs.get(mid)
        ko = fl["kickoff_utc"]
        rep = err = None
        if fl["phase"] in PRICED_PHASES and lock:
            try:
                rep = build_fn(mid)
            except Exception as e:  # noqa: BLE001 - shown on the page, not hidden
                err = f"{type(e).__name__}: {e}"
        mkt = _market_view(rep) if rep else None
        valid = rec is not None and "direction" in rec
        stake = (rec or {}).get("stake_frac") or 0
        conclusion = ("BET" if valid and stake > 0 else "NO_BET" if valid
                      else "LEGACY" if rec is not None else "PENDING")
        row = {"match_id": mid, "home": f["home_team"], "away": f["away_team"],
               "kickoff": ko, "phase": fl["phase"], "lost": fl["lost"],
               "deadline": fl["j_deadline_utc"], "finish_by": fl["finish_by_utc"],
               "at_risk": fl["at_risk"], "m": (lock or {}).get("m"), "j": (lock or {}).get("j"),
               "k_valid": (lock or {}).get("k_valid"), "market": mkt,
               "gap_pp": ((rep or {}).get("gate") or {}).get("gap_used_pp"),
               "rec": rec if valid else None, "conclusion": conclusion,
               "rank": judgment.rank_key(rec) if valid else -1.0,
               "report_error": err,
               "report_gaps": list(((rep or {}).get("honesty") or {}).get("gaps") or []),
               "earliest_price": None}
        row["flags"] = _flags(config, row, rec, lock, mkt)
        rows.append(row)

    for r in rows:
        if r["phase"] == "J_LOCKED" and r["kickoff"]:
            r["earliest_price"] = _earliest_price(now, r["kickoff"])

    bets = [r for r in rows if r["conclusion"] == "BET"]
    declined = [r for r in rows if r["conclusion"] == "NO_BET"]
    nearest = max((r for r in declined if r["gap_pp"] is not None),
                  key=lambda r: r["gap_pp"], default=None)
    # a gap common to every priced fixture belongs to the round, not to each row
    priced = [r for r in rows if r["market"]]
    common = ([g for g in priced[0]["report_gaps"] if all(g in r["report_gaps"] for r in priced)]
              if priced else [])
    return {"name": comp.get("name", config.competition_id), "gameweek": gameweek,
            "now": now, "rows": rows, "bets": bets, "declined": declined,
            "pending": [r for r in rows if r["conclusion"] in ("PENDING", "LEGACY") and not _missed(r)],
            "missed": [r for r in rows if r["conclusion"] in ("PENDING", "LEGACY") and _missed(r)],
            "nearest": nearest, "bet_pp": config.gate.bet_pp,
            "known_biases": list(config.known_biases), "common_gaps": common,
            "track_record": _track_record_line(config.era),
            "ledger_note": note_fn()}


def _track_record_line(era):
    from football_ai.decision import track_record
    try:
        return track_record.render(track_record.scorecard(era))
    except Exception as e:  # the summary must still render
        return f"研判實績(實績路徑):無法計算({type(e).__name__}: {e})"


def text_summary(data):
    n = len(data["rows"])
    missed = f"、已錯過 {len(data['missed'])} 場" if data["missed"] else ""
    return (f"{data['name']} 第 {data['gameweek']} 輪:下注 {len(data['bets'])} 場、"
            f"不下注 {len(data['declined'])} 場、待處理 {len(data['pending'])} 場{missed}(共 {n} 場)")


# --- rendering --------------------------------------------------------------

def _e(x):
    return html.escape(str(x), quote=True)


def _z(dt):
    return dt.astimezone(timezone.utc).strftime("%m-%d %H:%MZ") if dt else "—"


def _iso_z(s):
    try:
        return _z(matchflow._parse(s))
    except (TypeError, ValueError):
        return _e(s or "—")


def _pct(p, nd=0):
    return "—" if p is None else f"{p * 100:.{nd}f}"


def _trio(probs, nd=0):
    return "—" if not probs else " · ".join(_pct(probs[k], nd) for k in OUTCOMES)


def _short(x, n=80):
    t = " ".join(str(x or "").replace("**", "").split())
    return t if len(t) <= n else t[:n].rstrip() + "…"


def _text(x):
    if x is None:
        return ""
    if isinstance(x, str):
        return x.replace("**", "")
    if isinstance(x, (list, tuple)):
        return "\n".join(f"- {_text(i)}" for i in x)
    return json.dumps(x, ensure_ascii=False, indent=1)


def _pick(row):
    """The leaned outcome as a phrase: a team's win, or the draw."""
    d = row["rec"]["direction"]
    return {"home": f"{row['home']} 勝", "away": f"{row['away']} 勝", "draw": "和局"}[d]


def _layers(row):
    """The four readings of the leaned outcome: (key, glyph, name, p)."""
    d = row["rec"]["direction"]
    out = [("m", "M", "M", (row["m"] or {}).get(d)),
           ("j", "J", "J", (row["j"] or {}).get(d)),
           ("r", "研", "研判", row["rec"]["judgment_probs_90"].get(d)),
           ("k", "市", "市場", ((row["market"] or {}).get("fair") or {}).get(d))]
    return [x for x in out if x[3] is not None]


def _cells(row):
    """The leaned outcome's probability in each layer, as four aligned numbers."""
    have = {key: p for key, _, _, p in _layers(row)}
    cell = lambda key, label: (f'<span class="n{" r" if key == "r" else ""}" data-l="{label}">'
                               f'{_pct(have.get(key))}</span>')
    return '<span class="nums">' + "".join(cell(k, l) for k, l in (("m", "M"), ("j", "J"), ("r", "研判"), ("k", "市場"))) + '</span>'


def _colhead():
    return ('<div class="cols"><span>開球</span><span>場次</span><span>研判傾向(%)</span>'
            '<span class="nums"><span>M</span><span>J</span><span>研判</span><span>市場</span></span><span>狀態</span></div>')


def _chip(kind, text):
    return f'<span class="chip {kind}">{_e(text)}</span>'


def _status(row):
    c = row["conclusion"]
    if c == "BET":
        return _chip("bet", "下注")
    if c == "NO_BET":
        return _chip("no", "不下注")
    return _chip("wait", PHASE_LABEL.get(row["phase"], row["phase"]))


def _edge(row):
    """Leaned-outcome probability minus the market's, in pp, or None."""
    rec = row["rec"]
    fair = ((row["market"] or {}).get("fair") or {}).get(rec["direction"])
    return None if fair is None else (rec["judgment_probs_90"][rec["direction"]] - fair) * 100


def _edge_label(row):
    return "對收盤" if (row["market"] or {}).get("closing") else "對現價"


def _price_line(row):
    rec, mkt = row["rec"], row["market"]
    ep = rec.get("entry_price") or {}
    if ep.get("decimal_odds"):
        at = f",{_iso_z(ep['at'])}" if ep.get("at") else ""
        return f"{ep['decimal_odds']:g} @ {_e(ep.get('book', '?'))}{at}"
    if mkt and mkt["odds"].get(rec["direction"]):
        d = rec["direction"]
        return f"{mkt['odds'][d]:g} @ {_e(mkt['book'].get(d, '?'))},{_iso_z(mkt['at'])}"
    return "—"


def _bet_card(row):
    rec = row["rec"]
    d = rec["direction"]
    ep = rec.get("entry_price") or {}
    odds = ep.get("decimal_odds")
    ev = rec.get("ev_at_entry")
    if ev is None and odds:
        ev = sizing.ev(rec["prob"], odds)
    edge = _edge(row)
    warn = "".join(f"<li>{_e(f)}</li>" for f in row["flags"]
                   if f.startswith("有注額") or "contaminated" in f)
    stake = rec["stake_frac"] * 100
    return (f'<article class="betcard"><div class="bet-main"><div>'
            f'<p class="bet-fx">{_e(row["home"])} v {_e(row["away"])} · 開球 {_z(row["kickoff"])}</p>'
            f'<h3>{_e(_pick(row))}</h3><p class="bet-price">{_price_line(row)}</p></div>'
            f'<div class="bet-stake"><b>{stake:.2f}%</b><span>資金</span></div></div>'
            f'<dl class="kv"><div><dt>研判機率</dt><dd>{_pct(rec["prob"], 1)}%</dd></div>'
            f'<div><dt>市場去水</dt><dd>{_pct(((row["market"] or {}).get("fair") or {}).get(d), 1)}%</dd></div>'
            f'<div><dt>差距</dt><dd>{"—" if edge is None else f"{edge:+.1f}pp"}</dd></div>'
            f'<div><dt>EV</dt><dd>{"—" if ev is None else f"{ev * 100:+.1f}%"}</dd></div>'
            f'<div><dt>信心</dt><dd>{_e(rec["confidence"])}</dd></div>'
            f'<div><dt>機制</dt><dd>{_e(rec.get("mechanism_type", "—"))}({_e(rec.get("mechanism_grade", "—"))})</dd></div></dl>'
            f'{f"<ul class=warn>{warn}</ul>" if warn else ""}</article>')


def _answer(data):
    bets, rows = data["bets"], data["rows"]
    if bets:
        head = f'<h2 class="answer">建議下注 {len(bets)} 場</h2>'
        body = "".join(_bet_card(r) for r in bets)
        return head + body + '<p class="note">記錄不含九項防錯檢核表結果,下單前向研判 session 索取。</p>'
    done = len(data["declined"])
    if not done:
        if data["pending"]:
            return (f'<h2 class="answer pending">尚無結論</h2>'
                    f'<p class="lede">{len(data["pending"])} 場尚未研判,不等於不下注。</p>')
        return (f'<h2 class="answer pending">本輪沒有研判記錄</h2>'
                f'<p class="lede">{len(data["missed"])} 場在開球前未能研判。</p>')
    n = data["nearest"]
    head = f'<h2 class="answer none">{"本輪目前無下注" if data["pending"] else "本輪無下注"}</h2>'
    if n is None:
        return head
    edge = _edge(n)
    return head + (f'<p class="lede">最接近門檻:{_e(n["home"])} v {_e(n["away"])}。J 高於市場最大 {n["gap_pp"]:.1f}pp'
                   f'(門檻 {data["bet_pp"]:g}pp);{_edge_label(n)} {"—" if edge is None else f"{edge:+.1f}pp"};'
                   f'機制等級 {_e(n["rec"].get("mechanism_grade", "—"))}。</p>')


def _progress(data):
    rows = data["rows"]
    n = len(rows)
    seg = [("bet", len(data["bets"]), "下注"), ("no", len(data["declined"]), "不下注"),
           ("wait", len(data["pending"]), "待處理"), ("miss", len(data["missed"]), "已錯過")]
    bar = "".join(f'<i class="s-{k}" style="flex:{c}"></i>' for k, c, _ in seg if c)
    legend = " ".join(f'<span><i class="d d-{k}"></i>{lbl} {c}</span>' for k, c, lbl in seg if c)
    return f'<div class="progress" role="img" aria-label="{_e(text_summary(data))}">{bar}</div><p class="legend">{legend}</p>'


def _todo(data):
    rows, now = data["rows"], data["now"]
    items = []
    owing = sorted((r for r in rows if r["deadline"]), key=lambda r: r["deadline"])
    if owing:
        u = owing[0]
        risk = "(時間不足)" if any(r["at_risk"] for r in owing) else ""
        items.append((u["deadline"], "階段一",
                      f'{len(owing)} 場待鎖定或待 J。最急:{u["home"]} v {u["away"]},'
                      f'建議 {_z(u["finish_by"])} 前完成,最晚 {_z(u["deadline"])} 前推送{risk}。'))
    waiting = [r for r in rows if r["phase"] == "J_LOCKED"]
    if waiting:
        t = min((r["earliest_price"] for r in waiting if r["earliest_price"]), default=None)
        items.append((t or now, "等價格",
                      f'{len(waiting)} 場 J 已鎖,價格由排程寫入,最早 {_z(t)} 起可做階段二。'
                      if t else f'{len(waiting)} 場 J 已鎖,等排程寫入價格。'))
    ready = [r for r in rows if r["phase"] == "PRICED"]
    if ready:
        items.append((now, "階段二", f'{len(ready)} 場已有價格,現在可做研判。'))
    lost = [r for r in rows if r["lost"]]
    if lost:
        items.append((now, "缺口", f'{len(lost)} 場已有永久缺口(見各場注意事項)。'))
    if not items:
        return ""
    li = "".join(f'<li><span class="when">{_z(t)}</span><b>{_e(k)}</b><span>{_e(s)}</span></li>'
                 for t, k, s in sorted(items, key=lambda i: i[0]))
    return f'<section><h2>下一步</h2><ol class="todo">{li}</ol></section>'


def _instrument_table(row):
    m, j, mk = row["m"], row["j"], (row["market"] or {}).get("fair")
    r = (row["rec"] or {}).get("judgment_probs_90")
    head = "".join(f"<th>{OUTCOME_LABEL[k]}</th>" for k in OUTCOMES)
    rows = [(n, p, False) for n, p in (("M", m), ("J", j), ("研判", r), ("市場去水", mk))]
    if mk and j:
        rows.append(("J − 市場(pp)", {k: (j[k] - mk[k]) * 100 for k in OUTCOMES}, True))
    if mk and r:
        rows.append(("研判 − 市場(pp)", {k: (r[k] - mk[k]) * 100 for k in OUTCOMES}, True))
    body = "".join(
        f'<tr class="{"diff" if diff else ""}"><th>{n}</th>'
        + "".join((f"<td>{p[k]:+.1f}</td>" if diff else f"<td>{_pct(p[k], 1)}</td>") if p else "<td>—</td>" for k in OUTCOMES)
        + "</tr>" for n, p, diff in rows)
    return f'<table class="inst"><thead><tr><th></th>{head}</tr></thead><tbody>{body}</tbody></table>'


def _prose(title, text, excerpt=None):
    """A long record field, collapsed to one line until asked for."""
    t = _text(text)
    return (f'<details class="prose"><summary><b>{title}</b><span>{_e(_short(excerpt or t, 90))}</span></summary>'
            f'<pre>{_e(t)}</pre></details>')


def _detail(row):
    rec = row["rec"]
    parts = [_instrument_table(row)]
    if row["market"]:
        mk = row["market"]
        kind = "收盤價" if mk["closing"] else "賽前價"
        at = _iso_z(mk["at"]) if mk["at"] else "—"
        seen = ((rec or {}).get("market_devigged") or {}).get((rec or {}).get("direction"))
        entry = ("" if seen is None else
                 f";研判當時看到的市場 {_pct(seen, 1)}%,研判對進場價 {(rec['prob'] - seen) * 100:+.1f}pp")
        parts.append(f'<p class="meta">{kind},{at},{mk["n_books"]} 家書口{entry}</p>')
    if rec:
        for title, key in (("理由", "basis"), ("與 J 的差異", "divergence_note"),
                           ("市場錯在何處", "market_error_mechanism")):
            if rec.get(key):
                parts.append(_prose(title, rec[key]))
        ab = rec.get("abstention")
        if ab:
            parts.append(_prose("棄權舉證", "\n".join(
                f"{lbl}:{_text(ab.get(k))}" for lbl, k in
                (("缺少", "missing"), ("翻轉條件", "flips_if"), ("最晚可知", "known_by")))))
        caps = rec.get("capabilities_used") or []
        if caps:
            parts.append(_prose("研判動用的能力", [f"{c.get('capability')}:{c.get('what')}" for c in caps],
                                excerpt="、".join(str(c.get("capability")) for c in caps)))
        meta = [f"模型 {rec.get('model', '—')}"]
        if rec.get("reviewer_models"):
            meta.append("查核 " + "、".join(rec["reviewer_models"]))
        if rec.get("lambda") is not None:
            meta.append(f"λ {rec['lambda']:g} · f* {rec.get('f_star', 0):g} · 注額 {(rec.get('stake_frac') or 0) * 100:.2f}% 資金")
        parts.append(f'<p class="meta">{_e(" ｜ ".join(meta))}</p>')
    if row["flags"]:
        parts.append('<h4>注意</h4><ul class="warn">' + "".join(f"<li>{_e(f)}</li>" for f in row["flags"]) + "</ul>")
    if row["deadline"]:
        parts.append(f'<p class="meta">J 死線 {_z(row["deadline"])}(建議 {_z(row["finish_by"])} 前完成)</p>')
    return "".join(parts)


def _row(row):
    rec = row["rec"]
    if rec:
        edge = _edge(row)
        lean = (f'<div class="lean"><b>{_e(_pick(row))}</b><span>{_pct(rec["prob"])}% · 信心 {_e(rec["confidence"])}'
                f'{"" if edge is None else f" · {_edge_label(row)} {edge:+.1f}pp"}</span></div>')
        viz = _cells(row)
    else:
        lean = ('<div class="lean muted">' + (f'<span>M {_trio(row["m"])}</span>' if row["m"] else "")
                + (f'<span>J {_trio(row["j"])}</span>' if row["j"] else "") + '</div>')
        viz = '<span class="nums">' + '<span class="n"></span>' * 4 + '</span>'
    flag = f'<span class="flag" title="{_e("；".join(row["flags"]))}">⚑ {len(row["flags"])}</span>' if row["flags"] else ""
    return (f'<details class="fx {row["conclusion"].lower()}"><summary>'
            f'<span class="ko">{_z(row["kickoff"])}</span>'
            f'<span class="teams">{_e(row["home"])} <i>v</i> {_e(row["away"])}</span>{lean}'
            f'{viz}<span class="st">{_status(row)}{flag}</span></summary>'
            f'<div class="body">{_detail(row)}</div></details>')


def _group(title, rows):
    if not rows:
        return ""
    return (f'<section><h2>{title} <small>{len(rows)}</small></h2>{_colhead()}'
            f'{"".join(_row(r) for r in rows)}</section>')


def _fine_print(data):
    items = ["機率皆為 90 分鐘 1X2;市場為去水後;「對收盤/對現價」= 研判 − 市場。差距小於 1pp 在去水方法的誤差內。"]
    items += [f"已知儀器偏誤:{b}" for b in data["known_biases"]]
    if data.get("track_record"):
        items.append(data["track_record"])
    items += list(data["common_gaps"])
    lost = [r for r in data["rows"] if r["lost"]]
    items += [f"永久缺口 {r['home']} v {r['away']}:{r['lost']}" for r in lost]
    li = "".join(f"<li>{_e(i)}</li>" for i in items)
    return f'<details class="fine"><summary>說明與已知限制</summary><ul>{li}</ul></details>'


CSS = """
:root{--bg:#F5F7F6;--surface:#fff;--ink:#14201E;--muted:#586864;--rule:#D8E0DD;
--accent:#0B5D57;--accent-w:#DCEFEB;--warn:#8A4B12;--warn-w:#F8EAD8;--bad:#8C2F3B;--bad-w:#F6E1E4;
--m:#77837F;--j:#2A64A8;--r:#0B5D57;--k:#B4561B;--f:system-ui,-apple-system,"Segoe UI","Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0D1413;--surface:#151F1E;--ink:#E4ECEA;--muted:#93A5A1;--rule:#26332F;
--accent:#5CC5BC;--accent-w:#10312E;--warn:#E0A869;--warn-w:#33240F;--bad:#E58B97;--bad-w:#35171B;--m:#8E9C98;--j:#6FA8E6;--r:#5CC5BC;--k:#E0904F}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 var(--f);-webkit-font-smoothing:antialiased}
.wrap{max-width:60rem;margin:0 auto;padding:1.25rem 1rem 4rem;display:flex;flex-direction:column;gap:1.6rem}
header{display:flex;flex-wrap:wrap;justify-content:space-between;gap:.25rem 1rem;align-items:baseline}
h1{font-size:1.05rem;margin:0;font-weight:700}header p{margin:0;color:var(--muted);font-size:.8rem}
h2{font-size:1rem;margin:0 0 .5rem;font-weight:700}h2 small{color:var(--muted);font-weight:500;margin-left:.3rem}
h2.answer{font-size:1.7rem;line-height:1.25;margin:0 0 .4rem;color:var(--accent)}h2.answer.none,h2.answer.pending{color:var(--ink)}
h3{margin:0;font-size:1.25rem}h4{margin:1rem 0 .25rem;font-size:.82rem;color:var(--muted)}
.lede{margin:.2rem 0 .8rem;max-width:62ch}.note,.hint,.meta{color:var(--muted);font-size:.82rem;margin:.5rem 0 0}
.banner{background:var(--bad-w);color:var(--bad);border:1px solid var(--bad);border-radius:.5rem;padding:.6rem .8rem;font-size:.85rem}
.progress{display:flex;height:.5rem;border-radius:1rem;overflow:hidden;background:var(--rule);margin-top:.9rem}
.progress i{display:block}.s-miss,.d-miss{background:var(--muted);opacity:.4}.s-bet,.d-bet{background:var(--accent)}.s-no,.d-no{background:var(--m)}.s-wait,.d-wait{background:var(--warn)}
.legend{display:flex;flex-wrap:wrap;gap:.2rem 1rem;margin:.4rem 0 0;font-size:.8rem;color:var(--muted)}.d{display:inline-block;width:.6rem;height:.6rem;border-radius:50%;margin-right:.3rem}
.betcard{background:var(--surface);border:1px solid var(--accent);border-left-width:.35rem;border-radius:.6rem;padding:.9rem 1rem;margin:.6rem 0}
.bet-main{display:flex;justify-content:space-between;gap:1rem;align-items:flex-start}.bet-fx{margin:0;color:var(--muted);font-size:.8rem}.bet-price{margin:.1rem 0 0;font-variant-numeric:tabular-nums}
.bet-stake{text-align:right}.bet-stake b{display:block;font-size:1.6rem;line-height:1.1;color:var(--accent);font-variant-numeric:tabular-nums}.bet-stake span{font-size:.75rem;color:var(--muted)}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(7.5rem,1fr));gap:.5rem 1rem;margin:.8rem 0 0}.kv.wide{grid-template-columns:repeat(auto-fit,minmax(14rem,1fr))}
.kv>div{min-width:0}.kv dt{font-size:.72rem;color:var(--muted)}.kv dd{margin:0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
ul.warn{background:var(--warn-w);color:var(--warn);border-radius:.4rem;padding:.5rem .8rem .5rem 1.6rem;margin:.7rem 0 0;font-size:.85rem}
.todo{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.45rem}.todo li{display:grid;grid-template-columns:6.2rem 4.4rem 1fr;gap:.5rem;align-items:baseline;font-size:.9rem}
.when{font-variant-numeric:tabular-nums;color:var(--muted)}
details.fx{background:var(--surface);border:1px solid var(--rule);border-radius:.5rem;margin:.4rem 0}details.fx[open]{border-color:var(--accent)}
details.fx.bet{border-left:.3rem solid var(--accent)}
summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:5.4rem minmax(9rem,1.2fr) minmax(8rem,1.1fr) repeat(4,3.1rem) 6.5rem;gap:.4rem .8rem;align-items:center;padding:.55rem .8rem}
summary::-webkit-details-marker{display:none}summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.ko{font-size:.78rem;color:var(--muted);font-variant-numeric:tabular-nums}.teams{font-weight:600}.teams i{font-style:normal;color:var(--muted);font-weight:400}
.nums{display:contents}.cols{display:grid;grid-template-columns:5.4rem minmax(9rem,1.2fr) minmax(8rem,1.1fr) repeat(4,3.1rem) 6.5rem;gap:.4rem .8rem;padding:0 calc(.8rem + 1px) .15rem;font-size:.72rem;color:var(--muted)}.cols .nums span,.n{text-align:right}
.n{font-variant-numeric:tabular-nums}.n.r{font-weight:700;color:var(--accent)}.n::before{content:attr(data-l);display:none}
.lean{display:flex;flex-direction:column;font-size:.82rem}.lean span{color:var(--muted);font-variant-numeric:tabular-nums}.lean.muted{gap:0}
.st{display:flex;flex-direction:column;align-items:flex-start;gap:.15rem}.flag{font-size:.75rem;color:var(--warn)}
.chip{display:inline-block;font-size:.75rem;font-weight:600;padding:.1rem .55rem;border-radius:1rem;border:1px solid var(--rule)}
.chip.bet{background:var(--accent);border-color:var(--accent);color:var(--surface)}.chip.no{color:var(--muted)}.chip.wait{background:var(--warn-w);color:var(--warn);border-color:transparent}
.body{padding:.2rem .9rem 1rem;border-top:1px solid var(--rule)}
pre{white-space:pre-wrap;word-break:break-word;font:inherit;font-size:.85rem;margin:0 0 .5rem;background:var(--bg);border-radius:.4rem;padding:.6rem .75rem;max-height:26rem;overflow:auto}
.prose{border-top:1px solid var(--rule);margin-top:.45rem}.prose:first-of-type{margin-top:.8rem}
.prose summary{display:flex;gap:.7rem;align-items:baseline;padding:.45rem 0;font-size:.85rem}.prose summary::before{content:"▸";color:var(--muted)}.prose[open]>summary::before{content:"▾"}
.prose summary b{flex:none}.prose summary span{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
table.inst{border-collapse:collapse;margin:.7rem 0 0;font-size:.85rem;font-variant-numeric:tabular-nums}.inst th,.inst td{padding:.2rem .9rem .2rem 0;text-align:left;font-weight:400}.inst thead th,.inst tbody th{color:var(--muted);font-size:.78rem}.inst tr.diff td{color:var(--muted)}
.fine summary{display:block;padding:0;color:var(--muted);font-size:.85rem}.fine ul{margin:.5rem 0 0;padding-left:1.2rem;color:var(--muted);font-size:.82rem}
@media(max-width:720px){summary{grid-template-columns:1fr auto}.ko{grid-column:1}.teams{grid-column:1;grid-row:2}.st{grid-column:2;grid-row:1 / span 2;align-items:flex-end}
.cols{display:none}.lean{grid-column:1 / -1}.nums{display:grid;grid-column:1 / -1;grid-template-columns:repeat(4,1fr);gap:.4rem}.n{text-align:left;font-size:.9rem}.n:empty{display:none}.n::before{display:block;font-size:.7rem;font-weight:400;color:var(--muted)}.todo li{grid-template-columns:5.6rem 1fr}.todo li b{display:none}}
"""


def render_html(data):
    gw = data["gameweek"]
    banner = (f'<div class="banner">{_e(data["ledger_note"].replace("**", "").strip())}</div>'
              if data["ledger_note"] else "")
    return (f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{_e(data["name"])} 第 {gw} 輪決策摘要</title><style>{CSS}</style></head><body><div class="wrap">'
            f'<header><h1>{_e(data["name"])} · 第 {gw} 輪</h1><p>帳本讀取時間 {_z(data["now"])} · 時間一律為 UTC</p></header>'
            f'{banner}<section>{_answer(data)}{_progress(data)}</section>{_todo(data)}'
            f'{_group("下注", data["bets"])}'
            f'{_group("不下注", sorted(data["declined"], key=lambda r: -r["rank"]))}'
            f'{_group("已錯過(開球前無研判)", data["missed"])}'
            f'{_group("待處理", sorted(data["pending"], key=lambda r: r["kickoff"] or datetime.max.replace(tzinfo=timezone.utc)))}'
            f'{_fine_print(data)}</div></body></html>')


def write(comp, gameweek, out=None, **kw):
    data = collect(comp, gameweek, **kw)
    if out is None:
        from football_ai.core.paths import repo_root
        out = Path(repo_root()) / "reports" / f"{comp['config'].competition_id}_gw{gameweek:02d}_summary.html"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(data), encoding="utf-8")
    return out, data


def _selfcheck():
    import tempfile

    from football_ai.competitions.epl_2026_27 import CONFIG

    fails = []
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    KO = "2026-10-10T14:00:00+00:00"
    era = CONFIG.era
    names = {"bet": "Bet<i>", "bad": "Bad", "no": "No", "wait": "Wait", "open": "Open"}
    fixtures = [{"match_id": f"t_{k}", "gameweek": 7, "home_team": n, "away_team": "Z",
                 "date": "2026-10-10", "kickoff_utc": KO} for k, n in names.items()]
    fixtures.append({"match_id": "t_gone", "gameweek": 7, "home_team": "Gone", "away_team": "Z",
                     "date": "2026-10-01", "kickoff_utc": "2026-10-01T14:00:00+00:00"})
    gap = {"t_bet": 12.0, "t_bad": 3.0, "t_no": 6.0}

    def fake_build(mid):
        return {"markets": [{"market": "1x2", "best_book": {o: "b" for o in OUTCOMES},
                             "odds": {"home": 2.1, "draw": 3.4, "away": 3.6}, "at": "2026-10-06T11:05:00+00:00",
                             "closing": False, "n_books": 1,
                             "market_fair": {"home": .45, "draw": .28, "away": .27}}],
                "gate": {"gap_used_pp": gap[mid]}, "honesty": {"gaps": ["single-book read"]}}

    def rec(mid, stake, grade, **extra):
        probs = {"home": .55, "draw": .25, "away": .20}
        r = {"match_id": mid, "direction": "home", "prob": .55, "judgment_probs_90": probs,
             "confidence": "medium", "j_status": "locked", "mechanism_grade": grade,
             "mechanism_type": "other", "stake_frac": stake, "model": "t",
             "entry_price": {"selection": "home", "book": "b", "decimal_odds": 2.1},
             "basis": "x", "divergence_note": "y", **extra}
        if not stake:
            r["abstention"] = {"missing": "m", "flips_if": "f", "known_by": "k"}
        return r

    old = store_io.STORE_DIR
    with tempfile.TemporaryDirectory() as td:
        store_io.STORE_DIR = Path(td)
        try:
            def lock(mid, j):
                pl = {"match_id": mid, "kickoff_utc": KO, "teams": {"home": "A", "away": "Z"},
                      "probs_90": {"home": .50, "draw": .26, "away": .24}}
                if j:
                    pl["j"] = {"probs_90": {"home": .52, "draw": .25, "away": .23}, "runner": {"k_valid": 3}}
                store_io.append("locks", {"version": 1, "payload": pl}, era=era, match_id=mid, version=1)

            for k in ("bet", "bad", "no", "wait"):
                lock(f"t_{k}", True)
            for k in ("bet", "bad", "no"):
                store_io.append("odds", {"match_id": f"t_{k}", "book": "b", "market": "1x2"},
                                era=era, match_id=f"t_{k}")
            store_io.append("judgments", rec("t_bet", 0.006, "verified", basis="<script>alert(1)</script>"),
                            era=era, match_id="t_bet")
            store_io.append("judgments", rec("t_bad", 0.004, "actively_excluded"), era=era, match_id="t_bad")
            store_io.append("judgments", rec("t_no", 0.0, "searched_not_found"), era=era, match_id="t_no")
            before = {k: len(store_io.read(k, era=era)) for k in ("locks", "odds", "judgments")}

            comp = {"config": CONFIG, "name": "T", "upcoming_fixtures": fixtures}
            kw = {"now": now, "build_fn": fake_build, "ledger_note_fn": lambda: None}
            d = collect(comp, 7, **kw)
            by = {r["match_id"]: r for r in d["rows"]}
            counts = (len(d["bets"]), len(d["declined"]), len(d["pending"]), len(d["missed"]))
            if counts != (2, 1, 2, 1):
                fails.append(f"conclusions: bets/declined/pending/missed should be 2/1/2/1, got {counts}")
            if "t_gone" not in [r["match_id"] for r in d["missed"]]:
                fails.append("a fixture past kickoff with no judgment is missed, not pending")
            if by["t_bet"]["flags"]:
                fails.append(f"a consistent bet must carry no flag: {by['t_bet']['flags']}")
            bad = " ".join(by["t_bad"]["flags"])
            if "機制等級" not in bad or "低於下注門檻" not in bad:
                fails.append(f"a stake the mechanism grade and gap do not license must be flagged: {bad!r}")
            if by["t_wait"]["phase"] != "J_LOCKED" or by["t_wait"]["earliest_price"] != \
                    datetime(2026, 10, 6, 12, 7, tzinfo=timezone.utc):
                fails.append(f"a J-locked fixture 98h out is entry-due at the next tick "
                             f"(10-06 12:07Z): {by['t_wait']['earliest_price']}")
            ko_far = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)
            if _earliest_price(now, ko_far) != datetime(2026, 10, 8, 10, 7, tzinfo=timezone.utc):
                fails.append("beyond the 100h horizon the entry price waits for the first "
                             "tick inside it (10-08 10:00Z -> 10:07Z)")
            if by["t_open"]["conclusion"] != "PENDING" or by["t_open"]["deadline"] is None:
                fails.append("an unlocked fixture is pending and owes a J deadline")
            if d["common_gaps"] != ["single-book read"]:
                fails.append(f"a gap shared by every priced fixture belongs to the round: {d['common_gaps']}")
            page = render_html(d)
            if "建議下注 2 場" not in page:
                fails.append("the answer must lead with the number of bets")
            if "<script>" in page or "&lt;script&gt;" not in page or "Bet<i>" in page:
                fails.append("record and team text must be escaped")
            if "尚無結論" in page or "<td>+" not in page:
                fails.append("a round with conclusions must not read as pending, and the table must "
                             "show signed per-outcome gaps")
            only = collect({**comp, "upcoming_fixtures": [f for f in fixtures if f["match_id"] in ("t_wait", "t_open")]}, 7, **kw)
            if "尚無結論" not in render_html(only) or "本輪目前無下注" in render_html(only):
                fails.append("a round with no conclusion must say pending, never 'no bet'")
            gone = render_html(collect({**comp, "upcoming_fixtures": [f for f in fixtures if f["match_id"] == "t_gone"]}, 7, **kw))
            if "尚無結論" in gone or "本輪沒有研判記錄" not in gone:
                fails.append("a round with only missed fixtures must not read as pending")
            after = {k: len(store_io.read(k, era=era)) for k in before}
            if after != before:
                fails.append(f"the summary must not write to the store: {before} -> {after}")
        finally:
            store_io.STORE_DIR = old
    return fails
