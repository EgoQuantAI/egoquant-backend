"""Re-check after the hardening deploy (20 Oct): read-only, about 20 requests, no portfolio build, no account, no LLM call.
usage: probe_after_deploy.py <base_url> <out.json>. Expected answers are in the EXPECT table at the end."""
import http.client
import json
import ssl
import sys
import time
import urllib.parse

BASE, OUT = sys.argv[1].rstrip("/"), sys.argv[2]
MAX = 27
VERCEL = "https://fintechaton-frontend.vercel.app"
SEC = ["x-content-type-options", "x-frame-options", "referrer-policy", "permissions-policy"]
SHOW = SEC + ["server", "strict-transport-security", "access-control-allow-origin", "access-control-allow-credentials",
              "access-control-allow-methods", "access-control-allow-headers", "retry-after", "cache-control", "content-type",
              "www-authenticate", "allow", "via", "x-powered-by"]
CANARIES = ("CANARY-ZZ-91", "CANARY-VAL-77", "CANARY-NUM-55", "extra_canary_field_99", "CANARY-CTX", "CANARY-TOKEN-33", "CANARY-TX")
count = 0
rows = []
parsed = urllib.parse.urlparse(BASE)


def request(pid, method, path, body=None, headers=None, chunked=False, raw_headers=None):
    global count
    if count >= MAX:
        raise SystemExit(f"budget of {MAX} requests used up at {pid}")
    count += 1
    conn = (http.client.HTTPSConnection(parsed.netloc, timeout=30, context=ssl.create_default_context()) if parsed.scheme == "https"
            else http.client.HTTPConnection(parsed.netloc, timeout=30))          # http only for a local dry run
    hdrs = {"User-Agent": "egoquant-selfcheck/1 (team; read-only)", **(headers or {})}
    t0 = time.time()
    try:
        if chunked:
            conn.putrequest(method, path)
            for k, v in {**hdrs, "Transfer-Encoding": "chunked"}.items():
                conn.putheader(k, v)
            conn.endheaders()
            data = body if isinstance(body, bytes) else (body or "").encode()
            conn.send(hex(len(data))[2:].encode() + b"\r\n" + data + b"\r\n0\r\n\r\n")
        elif raw_headers:
            conn.putrequest(method, path, skip_accept_encoding=True)
            for k, v in {**hdrs, **raw_headers}.items():
                conn.putheader(k, v)
            conn.endheaders()
            conn.send(body if isinstance(body, bytes) else (body or "").encode())
        else:
            data = body if isinstance(body, bytes) else (body.encode() if isinstance(body, str) else None)
            conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        text = r.read(200_000).decode("utf-8", "replace") if method != "HEAD" else ""
        status, rh = r.status, {k.lower(): v for k, v in r.getheaders()}
    except Exception as err:  # noqa: BLE001
        status, rh, text = 0, {}, f"{type(err).__name__}: {err}"[:200]
    finally:
        conn.close()
    echo = [c for c in CANARIES if c in text]
    rec = {"id": pid, "req": f"{method} {path[:60]}", "status": status, "secs": round(time.time() - t0, 2), "echo": echo,
           "traceback": any(m in text for m in ("Traceback", 'File "', "site-packages")), "len": len(text),
           "missing_sec": [h for h in SEC if h not in rh] if status else SEC, "headers": {h: rh[h] for h in SHOW if h in rh},
           "ratelimit": {k: v for k, v in rh.items() if "ratelimit" in k or k == "retry-after"}, "body": text[:170].replace("\n", " "), "text": text[:4000]}
    rows.append(rec)
    time.sleep(0.12)
    return rec


J = {"Content-Type": "application/json"}
EXPECT = {}


def check(pid, ok, expected):
    EXPECT[pid] = {"ok": bool(ok), "expected": expected}


r = request("A01", "GET", "/data/status")
body_text = r["text"]
check("A01 /data/status shows the library versions and price mode", r["status"] == 200 and "libraries" in body_text and "price_mode" in body_text, "200 with libraries and price_mode")
r = request("A02", "GET", "/health/")
check("A02 /health/ is not a redirect", r["status"] in (200, 404) and "location" not in r["headers"], "200 or 404, no Location header")
r = request("A03", "GET", "/health")
check("A03 HSTS on every answer", "strict-transport-security" in r["headers"], "Strict-Transport-Security present")
r = request("A04", "POST", "/debug/portfolio", "{}", J)
check("A04 /debug/portfolio is a 404 even for an empty body", r["status"] == 404, "404")
r = request("A05", "GET", "/account/me", None, {"Authorization": "Bearer garbage"})
check("A05 a garbage token is a 401", r["status"] == 401, "401")
r = request("A06", "GET", "/account/me")
check("A06 no token is a 401", r["status"] == 401, "401")
r = request("A07", "POST", "/profile", "{}", J)
check("A07 a refused body is a plain 422 with sentences", r["status"] == 422 and not r["echo"], "422, nothing echoed")
r = request("A08", "POST", "/profile", json.dumps({"choices": {"CANARY-ZZ-91": "CANARY-VAL-77"}, "amount_invested": "CANARY-NUM-55", "amount_needed": 1, "extra_canary_field_99": "X"}), J)
check("A08 planted values are not echoed", r["status"] == 422 and not r["echo"], "422, no canary in the answer")
r = request("A09", "POST", "/portfolio", json.dumps({"answers": {"choices": {"CANARY-ZZ-91": "CANARY-VAL-77"}}, "extra_canary_field_99": 1}), J)
check("A09 /portfolio refuses an invalid body without building", r["status"] == 422 and not r["echo"], "422, no canary")
r = request("A10", "POST", "/trade-check", json.dumps({"answers": "CANARY-VAL-77", "planned": "CANARY-VAL-77"}), J)
check("A10 /trade-check refuses an invalid body", r["status"] == 422 and not r["echo"], "422, no canary")
r = request("A11", "POST", "/profile", '{"x": 1}', {**J, "Content-Length": "1100000"}, raw_headers={"Content-Length": "1100000"})
check("A11 a declared body over the limit is a 413", r["status"] == 413, "413")
r = request("A12", "POST", "/profile", '{"choices": {}}', J, chunked=True)
check("A12 a chunked body is a 411", r["status"] == 411, "411")
r = request("A13", "OPTIONS", "/profile", None, {"Origin": VERCEL, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
check("A13 CORS allows the Vercel origin", r["status"] == 200 and r["headers"].get("access-control-allow-origin") == VERCEL, "200 and the origin echoed")
r = request("A14", "OPTIONS", "/profile", None, {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
check("A14 CORS refuses another origin", r["status"] == 400 and "access-control-allow-origin" not in r["headers"], "400, no allow-origin")
r = request("A15", "GET", "/nope")
check("A15 404 carries the security headers", r["status"] == 404 and not r["missing_sec"], "404 and four security headers")
r = request("A16", "PUT", "/profile", "{}", J)
check("A16 405 carries the security headers", r["status"] == 405 and not r["missing_sec"], "405 and four security headers")
r = request("A17", "POST", "/what-if", json.dumps({"what_if_context": "x", "scenario": {}}), J)
check("A17 /what-if without a token is a 401", r["status"] == 401, "401")
r = request("A18", "POST", "/verify", json.dumps({"extra_canary_field_99": "CANARY-VAL-77"}), J)
check("A18 /verify with garbage is a plain refusal", r["status"] == 422 and not r["echo"], "422, no canary")
json.dump({"count": count, "rows": rows, "expect": EXPECT}, open(OUT, "w"), indent=1)
by_id = {r["id"]: r for r in rows}
print()
print("VERDICT (one line per check; PASS means the live system answered as designed)")
for label, v in EXPECT.items():
    pid = label.split()[0]
    got = by_id[pid]["status"]
    detail = f"got HTTP {got}" + (f", planted value echoed: {by_id[pid]['echo']}" if by_id[pid]["echo"] else "")
    print(f"{'PASS' if v['ok'] else 'FAIL'}  {label}  (expected {v['expected']}; {detail})")
bad = [k for k, v in EXPECT.items() if not v["ok"]]
print()
print(f"{len(EXPECT) - len(bad)} of {len(EXPECT)} checks pass; {count} requests used. " + ("Failed: " + "; ".join(bad) if bad else "Nothing failed."))
