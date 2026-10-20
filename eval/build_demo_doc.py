"""Builds docs/DEMO_SCRIPT.md from eval/demo_script.template.md and docs/demo_data/demo_numbers.json.

    python eval/demo_pack.py --out docs/demo_data/demo_numbers.json     # run the three scenarios headless (offline)
    python eval/build_demo_doc.py                                       # fill the template with what that run produced

Two variants, one per portfolio method, sit side by side so that choosing the default is copying files, not a rebuild (the demo video script, docs/DEMO_VIDEO_SCRIPT*.md from eval/demo_video.template.md via --template, follows the same pattern):
    docs/demo_data/demo_numbers_v1.json + docs/DEMO_SCRIPT_v1.md        (flag unset)
    docs/demo_data/demo_numbers_v2.json + docs/DEMO_SCRIPT_v2.md        (EGOQUANT_PORTFOLIO_METHOD=v2)
    docs/demo_data/demo_numbers.json    + docs/DEMO_SCRIPT.md           (the default: a copy of one pair)
    EGOQUANT_PORTFOLIO_METHOD=v2 python eval/demo_pack.py --out docs/demo_data/demo_numbers_v2.json
    python eval/build_demo_doc.py --numbers docs/demo_data/demo_numbers_v2.json --out docs/DEMO_SCRIPT_v2.md
The wording that depends on the method sits in <!--v1-->...<!--/v1--> and <!--v2-->...<!--/v2--> blocks of the template;
the block of the method recorded in the numbers (_meta.method, "v1" when absent) is kept, the other one dropped.

Every number, answer and verdict in the document is read off the run; the template holds only the wording. A
placeholder {{saver.chance}} is looked up in the fragments below; an unknown placeholder stops the build.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NUMBERS = ROOT / "docs" / "demo_data" / "demo_numbers.json"
TEMPLATE = ROOT / "eval" / "demo_script.template.md"
OUT = ROOT / "docs" / "DEMO_SCRIPT.md"

CLASS_LABELS = [("equity_weight", "Stocks"), ("bond_weight", "Bonds"), ("cash_weight", "Cash"),
                ("commodity_weight", "Gold & commodities"), ("crypto_weight", "Crypto")]
# The words an investor reads for a verdict (egoquant/trade_check.py VERDICT_WORDS, since 20 Oct: one vocabulary on the page, in the
# explanation and in the chat). The codes advisable / allowed_with_warnings / not_advisable stay the API values in the pack.
VERDICT = {"advisable": "within your limits", "allowed_with_warnings": "within your limits, with warnings",
           "not_advisable": "outside your limits"}


def pct(x, digits=0):
    return f"{x * 100:.{digits}f}%"


def money(x):
    return f"{x:,.0f}"


def mix(w):
    return " · ".join(f"{label} {pct(w[key])}" for key, label in CLASS_LABELS if (w.get(key) or 0) > 0.0005)


def top_holdings(w, n=6):
    return ", ".join(f"{h['id']} {pct(h['weight'])}" for h in w["holdings"][:n])


def table(rows, header):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in rows]
    return "\n".join(out)


def answers_table(s):
    rows = []
    for a in s["answers"]:
        q = a["question"] if len(a["question"]) <= 95 else a["question"][:92] + "..."
        rows.append((a["id"], q, "**" + a["answer"] + "**"))
    return table(rows, ["#", "Question (shortened)", "Click / type"])


def prefs_lines(s):
    lines = []
    for p in s["preferences"]:
        if p["answer"]:
            lines.append(f"- {p['id']} \"{p['question']}\" tick: " + "; ".join(f"**{a}**" for a in p["answer"]))
    skipped = [p["id"] for p in s["preferences"] if not p["answer"]]
    lines.append(f"- Leave empty / skip: {', '.join(skipped)}.")
    return "\n".join(lines)


def not_offered_lines(s):
    out = []
    for p in s["preferences"]:
        for n in p["not_offered"][:3]:
            out.append(f"- {p['id']}: \"{n['option']}\" is greyed out: {n['reason']}.")
    return "\n".join(out[:4]) or "- (every option is offered)"


def trade_fragment(prefix, t, f):
    p = t["planned"]
    f[prefix + ".planned"] = f"{p['side'].capitalize()} {money(p['amount'])} of {p['ticker']}"
    f[prefix + ".verdict"] = VERDICT[t["verdict"]]
    f[prefix + ".reasons"] = "\n".join(f"  - *{r['severity']}*: {r['message']}" for r in t["reasons"])
    f[prefix + ".direction"] = "\n".join(f"  - {d}" for d in t.get("direction_parts") or []) or "  - (no change in the three measures)"
    f[prefix + ".template"] = "\n".join("  > " + line for line in t["template_reply"].splitlines())


def what_if_fragment(prefix, w, f):
    f[prefix + ".question"] = w["question"]
    f[prefix + ".status"] = w["status"]
    if w["status"] == "ok":
        f[prefix + ".result"] = (f"goal chance {w['baseline']['goal_chance']} → {w['scenario_outcome']['goal_chance']} "
                                 f"({w['baseline_goal_frequency']} → {w['scenario_goal_frequency']} in 100 simulated futures)")
    else:
        f[prefix + ".result"] = f"**{w['status']}**: {w['reason']}"
    f[prefix + ".scenario_text"] = w["scenario_text"]


def game_fragments(key, s, pr, pg):
    """The Risk Game step of a scenario: what to play, what the app reports, and what it did to the category."""
    out = {}
    game = (s.get("intake") or {}).get("game")
    if not game or not pg.get("game_played"):
        for name in ("game_step", "game_effect", "expected_category", "game_explanation"):
            out[f"{key}.{name}"] = "" if name != "expected_category" else pr["c_level"]
        return out
    was, now = pr["c_level"], pg["c_level"]
    if now == was:
        effect = f"it does not change the category: {now}"
        expected = f"{now} (the questionnaire alone gives {was}; the Risk Game agrees)"
    elif int(now[1:]) < int(was[1:]):
        effect = f"it LOWERS the category from {was} to {now}"
        expected = f"{was} from the questionnaire, {now} after the Risk Game"
    else:
        effect = f"it RAISES the category from {was} to {now}"
        expected = f"{was} from the questionnaire, {now} after the Risk Game"
    out[f"{key}.game_effect"] = effect
    out[f"{key}.expected_category"] = expected
    out[f"{key}.game_explanation"] = "\n".join(f"  > {line}" for line in pg["explanation"] if "Risk Game" in line or "game" in line)
    out[f"{key}.game_step"] = (
        f"**1b. Risk Game** (menu *Risk Game*, about 2 minutes; everyone is offered it and skipping needs a confirmation). {game['how'].capitalize()}. "
        f"The app reports a tolerance of **{game['tolerance']}** and a loss acceptance of **{game['loss_acceptance']}** out of 100 "
        f"(the pack assumed exactly these; the loss acceptance stays at 50 unless a big loss comes up in your rounds, and your own play can "
        f"differ by a few points).\n\n"
        f"Expected after the game: game level **{pg['game_level']}** of 5 (score {pg['game_score']:.0f}), blended attitude level "
        f"**{pg['blended_level']}** (two thirds questionnaire, one third game), the financial situation caps at level "
        f"**{pg['capacity_level']}**, so the category is the lower of the two: **{now}**. In short, {effect}.\n"
        f"{out[f'{key}.game_explanation']}")
    basis = ((s.get("with_ibkr") or {}).get("category_basis") or {}).get("text")
    if basis:                                      # the sentence the portfolio report itself prints (report.category_basis)
        out[f"{key}.game_step"] += f"\nThe portfolio report (Method tab) says the same in one sentence:\n  > {basis}"
    return out


def fragments(d):
    sys.path.insert(0, str(ROOT))
    from egoquant.advisor.trade_explain import AI_UNAVAILABLE
    method = d["_meta"].get("method", "v1")
    as_of = d["_meta"].get("as_of") or ""
    pinned = len(as_of) == 10 and as_of[4] == "-"          # a date, not "the last weekday before the run"
    f = {"meta.prices": d["_meta"]["prices"], "meta.file": d["_meta"]["prices_file"], "ai.sentence": AI_UNAVAILABLE,
         "meta.method": method,
         "meta.as_of_note": (f"The run's decision date is pinned to {as_of} (the last close in the saved prices) so that it can be "
                             "repeated; the live app uses the last weekday before the day it is used, so its page may show a later "
                             "date with the same prices." if pinned else ""),
         "meta.method_note": ("Portfolio method: v2 (the backend must run with EGOQUANT_PORTFOLIO_METHOD=v2)." if method == "v2"
                              else "Portfolio method: the default one (v1).")}
    for key in ("saver", "balanced", "beginner"):
        s = d[key]
        pr = s["profile"]                                  # the questionnaire alone
        pg = s.get("profile_with_game") or pr              # after the Risk Game (the same profile in an older pack)
        f[f"{key}.persona"] = s["persona"]
        f[f"{key}.c"] = pg["c_level"]
        f[f"{key}.q_class"] = pr["c_level"]
        f.update(game_fragments(key, s, pr, pg))
        f[f"{key}.score"] = str(pr["tolerance_score"])
        f[f"{key}.tol"] = str(pr["tolerance_level"])
        f[f"{key}.cap"] = str(pr["capacity_level"])
        f[f"{key}.explanation"] = "\n".join(f"  > {line}" for line in pr["explanation"])
        f[f"{key}.maxr"] = str(pg["max_r_level"])
        f[f"{key}.answers_table"] = answers_table(s)
        f[f"{key}.prefs"] = prefs_lines(s)
        f[f"{key}.not_offered"] = not_offered_lines(s)
        f[f"{key}.platform"] = ", ".join(s["platforms"])
        i = s["intake"]
        f[f"{key}.horizon"], f[f"{key}.liquidity"] = f"{i['horizon']:g}", pct(i["liquidity"])
        f[f"{key}.mincash"], f[f"{key}.goal"] = pct(i["min_cash"]), money(i["goal_amount"])
        f[f"{key}.monthly"] = money(i["monthly_flow"]) if i["monthly_flow"] else "nothing (leave empty)"
        invested = float(next(a for a in s["answers"] if a["id"] == "B3_invested")["answer"].replace(",", ""))
        f[f"{key}.invested"] = money(invested)
        for variant, name in (("without_ibkr", "plain"), ("with_ibkr", "ibkr")):
            w = s[variant]
            p = f"{key}.{name}"
            f[p + ".status"] = "Approved" if w["approved"] else "Rejected"
            f[p + ".chance"] = w["goal"]["chance"]
            f[p + ".in100"] = str(w["goal"]["in_100"])
            f[p + ".stocks"], f[p + ".vol"] = pct(w["equity_weight"]), pct(w["volatility"], 1)
            f[p + ".worst"], f[p + ".mix"] = pct(w["worst_loss"]), mix(w)
            f[p + ".holdings"], f[p + ".headline"] = top_holdings(w), w["headline"]
            f[p + ".n"] = str(w["n_instruments"])
            f[p + ".checks"] = f"{w['risk_engine_checks']['passed']} of {w['risk_engine_checks']['total']}"
            f[p + ".worst_scenario"] = w["worst_scenario"]
            f[p + ".limits"] = "\n".join(
                f"  - {'✓' if l['ok'] else '✗'} {l['name']}: {pct(l['value'], 1)} {'≥' if l['direction'] == 'min' else '≤'} "
                f"{pct(l['limit'], 1)}  (from {l['source']})" for l in w["limits"])
        before, after = s["without_ibkr"]["goal"]["in_100"], s["with_ibkr"]["goal"]["in_100"]
        f[f"{key}.cost_phrase"] = (f"the goal chance fell from {s['without_ibkr']['goal']['chance']} to {s['with_ibkr']['goal']['chance']}"
                                   if after < before else
                                   f"here it cost no goal chance ({s['with_ibkr']['goal']['chance']} either way): the portfolio only "
                                   "became more cautious")
        beh = [l for l in s["with_ibkr"]["limits"] if "behavioural" in l["name"].lower()]
        f[f"{key}.behavioural"] = (f"{beh[0]['name']}: {pct(beh[0]['value'], 1)} ≤ {pct(beh[0]['limit'], 1)}" if beh
                                   else "no behavioural limit (the history gives no reason to be more careful than the category)")
        f[f"{key}.behav_limit"] = pct(beh[0]["limit"], 1) if beh else "n/a"
        class_loss = next((l for l in s["without_ibkr"]["limits"] if l["name"] == "Loss in a severe fall"), None)
        f[f"{key}.class_loss_limit"] = pct(class_loss["limit"]) if class_loss else "n/a"
        f[f"{key}.banner_prices"] = next((b for b in s["with_ibkr"]["banners"] if b.startswith("LIVE PRICES")), "")[:140]
        for n, t in enumerate(s["trade_checks"]):
            trade_fragment(f"{key}.t{n}", t, f)
        for n, w in enumerate(s["what_ifs"]):
            what_if_fragment(f"{key}.w{n}", w, f)
    return f


def select_method(template: str, method: str) -> str:
    """Keep the blocks written for `method`, drop the others: <!--v1-->...<!--/v1--> and <!--v2-->...<!--/v2-->."""
    return re.sub(r"<!--(v[12])-->(.*?)<!--/\1-->", lambda m: m.group(2) if m.group(1) == method else "", template,
                  flags=re.DOTALL)


def build(numbers: Path = NUMBERS, template_path: Path = TEMPLATE) -> str:
    d = json.loads(Path(numbers).read_text(encoding="utf-8"))
    f = fragments(d)
    template = select_method(Path(template_path).read_text(encoding="utf-8"), d["_meta"].get("method", "v1"))

    def fill(m):
        key = m.group(1).strip()
        if key not in f:
            sys.exit(f"unknown placeholder {{{{{key}}}}} in {Path(template_path).name}")
        return f[key]

    return re.sub(r"\{\{([^}]+)\}\}", fill, template)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--numbers", default=str(NUMBERS), help="the run to read (default: docs/demo_data/demo_numbers.json)")
    parser.add_argument("--out", default=str(OUT), help="the document to write (default: docs/DEMO_SCRIPT.md)")
    parser.add_argument("--template", default=str(TEMPLATE),
                        help="the template to fill (default: eval/demo_script.template.md; the video script is "
                             "eval/demo_video.template.md -> docs/DEMO_VIDEO_SCRIPT.md)")
    args = parser.parse_args()
    out = Path(args.out)
    out.write_text(build(Path(args.numbers), Path(args.template)), encoding="utf-8")
    print("written", out.resolve().relative_to(ROOT))
