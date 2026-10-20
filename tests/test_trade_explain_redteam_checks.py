"""The trade-explanation red-team (eval/trade_explain_redteam.py, live, waits for Savas's go) judges replies with `evaluate`. Offline:
a template reply passes every check for each of the three verdicts, and a reply with the check's own label ("advisable") or a soft
advice wording fails the check meant for it. Run in a separate process (the script reads .env and puts the repository on the path)."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DRIVER = textwrap.dedent('''
    import importlib.util, json, os, sys
    from types import SimpleNamespace
    ROOT = r"{root}"
    sys.path.insert(0, ROOT)
    os.environ["EGOQUANT_CONTEXT_SECRET"] = "test-secret"
    spec = importlib.util.spec_from_file_location("te_redteam", ROOT + "/eval/trade_explain_redteam.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    te = module.te
    out = {{}}
    for verdict, check in module.build_checks():
        facts = te.trade_facts(check)
        text = te._template(facts)
        good = SimpleNamespace(text=text, verdict=verdict, source="template")
        out[verdict] = {{"good": module.evaluate(good, facts, text),
                         "label": module.evaluate(SimpleNamespace(text=text + " That is advisable.", verdict=verdict, source="template"),
                                                  facts, text),
                         "soft": module.evaluate(SimpleNamespace(text=text + " You could sell half of it.", verdict=verdict,
                                                                 source="template"), facts, text),
                         "headline": text.splitlines()[0]}}
    print("RESULT " + json.dumps(out))
''')


def test_the_template_passes_and_the_old_labels_and_soft_advice_are_caught(tmp_path):
    script = tmp_path / "driver.py"
    script.write_text(DRIVER.format(root=str(ROOT)), encoding="utf-8")
    done = subprocess.run([sys.executable, str(script)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=600)
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, done.stdout[-1500:] + done.stderr[-1500:]
    results = json.loads(line[len("RESULT "):])
    assert set(results) == {"not_advisable", "allowed_with_warnings", "advisable"}
    for verdict, row in results.items():
        assert all(row["good"].values()), (verdict, [k for k, v in row["good"].items() if not v])
        assert row["label"]["no_verdict_label_words"] is False and row["label"]["no_soft_advice"] is True, verdict
        assert row["soft"]["no_soft_advice"] is False and row["soft"]["no_verdict_label_words"] is True, verdict
    assert results["not_advisable"]["headline"].startswith("This trade is outside your limits")
    assert results["advisable"]["headline"].startswith("This trade stays within your limits")
