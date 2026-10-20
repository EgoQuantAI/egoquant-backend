"""Read-only live probes against the team's own deployment. Validation paths only: no portfolio build, no account, no LLM call.
Hard budget: at most MAX requests in total (counted here). usage: probe.py <base_url> <out.json>"""
import http.client
import json
import ssl
import sys
import time
import urllib.parse

BASE, OUT = sys.argv[1].rstrip("/"), sys.argv[2]
MAX = 115
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
    conn = http.client.HTTPSConnection(parsed.netloc, timeout=30, context=ssl.create_default_context())
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
           "ratelimit": {k: v for k, v in rh.items() if "ratelimit" in k or k == "retry-after"}, "body": text[:170].replace("\n", " ")}
    rows.append(rec)
    time.sleep(0.12)
    return rec


J = {"Content-Type": "application/json"}
# ---- basics, docs, public GETs
request("P01", "GET", "/")
request("P02", "GET", "/health")
request("P03", "HEAD", "/health")
request("P04", "OPTIONS", "/health")
request("P05", "GET", "/docs")
request("P06", "GET", "/openapi.json")
request("P07", "GET", "/redoc")
request("P08", "GET", "/data/status")
request("P09", "GET", "/anchoring/status")
request("P10", "GET", "/questionnaire")
request("P11", "GET", "/portfolio/options")
request("P12", "POST", "/debug/portfolio", "{}", J)
# ---- 404 / 405 / odd paths
request("P13", "GET", "/nope")
request("P14", "GET", "/portfolio")
request("P15", "PUT", "/profile", "{}", J)
request("P16", "DELETE", "/profile")
request("P17", "PATCH", "/profile", "{}", J)
request("P18", "TRACE", "/")
request("P19", "GET", "//")
request("P20", "GET", "/health/")
request("P21", "GET", "/%2e%2e/%2e%2e/etc/passwd")
request("P22", "GET", "/?q=%3Cscript%3Ealert(1)%3C/script%3E")
# ---- validation: 422 must not echo
request("P23", "POST", "/profile", "{}", J)
request("P24", "POST", "/profile", '{"answers": ', J)
request("P25", "POST", "/profile", json.dumps({"choices": {"CANARY-ZZ-91": "CANARY-VAL-77"}, "amount_invested": "CANARY-NUM-55",
                                              "amount_needed": 1, "extra_canary_field_99": "X"}), J)
request("P26", "POST", "/profile", "CANARY-VAL-77", {"Content-Type": "text/plain"})
request("P27", "POST", "/profile", "[]", J)
request("P28", "POST", "/profile", '{"choices": {}, "amount_invested": NaN, "amount_needed": Infinity}', J)
request("P29", "POST", "/portfolio", json.dumps({"answers": {"choices": {"CANARY-ZZ-91": "CANARY-VAL-77"}}, "extra_canary_field_99": 1}), J)
request("P30", "POST", "/preferences/form", json.dumps({"answers": "CANARY-VAL-77"}), J)
request("P31", "POST", "/chat", json.dumps({"message": 12345, "chat_context": "CANARY-CTX", "extra_canary_field_99": 1}), J)
request("P32", "POST", "/trade-check", json.dumps({"answers": "CANARY-VAL-77", "planned": "CANARY-VAL-77"}), J)
request("P33", "POST", "/verify", json.dumps({"extra_canary_field_99": "CANARY-VAL-77"}), J)
request("P34", "POST", "/verify", "CANARY-VAL-77 not json", J)
request("P35", "GET", "/verify/tx/CANARY-TX")
request("P36", "POST", "/briefing/start", json.dumps({"briefing_context": "CANARY-CTX"}), J)
request("P37", "GET", "/briefing/CANARY-VAL-77")
request("P38", "POST", "/portfolio/ibkr-check", json.dumps({"token": "CANARY-TOKEN-33"}), J)
# ---- size limits and framing
request("P39", "POST", "/profile", '{"x": "' + "a" * 1_100_000 + '"}', J)
request("P40", "POST", "/chat", '{"x": "' + "a" * 600_000 + '"}', J)
request("P41", "POST", "/portfolio", '{"x": "' + "a" * 4_600_000 + '"}', J)
request("P42", "POST", "/profile", '{"choices": {}}', J, chunked=True)
request("P43", "POST", "/profile", "{}", J, raw_headers={"Content-Length": "abc"})
# ---- CORS
request("P44", "OPTIONS", "/profile", None, {"Origin": VERCEL, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
request("P45", "OPTIONS", "/profile", None, {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
request("P46", "GET", "/health", None, {"Origin": "https://evil.example"})
request("P47", "GET", "/health", None, {"Origin": VERCEL})
request("P48", "OPTIONS", "/profile", None, {"Origin": VERCEL, "Access-Control-Request-Method": "PUT"})
request("P49", "OPTIONS", "/profile", None, {"Origin": VERCEL, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-custom-header"})
request("P50", "OPTIONS", "/profile", None, {"Origin": "null", "Access-Control-Request-Method": "POST"})
# ---- authentication: plain 401, never a 5xx
for i, (m, p, b) in enumerate([("GET", "/account/me", None), ("GET", "/account/reports", None), ("GET", "/account/profile", None),
                               ("GET", "/account/onboarding", None), ("PUT", "/account/onboarding", "{}"), ("POST", "/account/portfolio", "{}"),
                               ("POST", "/account/trade-check", "{}"), ("DELETE", "/account/data", None), ("DELETE", "/account", None),
                               ("GET", "/account/reports/00000000-0000-0000-0000-000000000000", None),
                               ("GET", "/account/reports/x/seal", None), ("GET", "/privacy-log/abcdefgh12", None)], start=51):
    request(f"P{i}", m, p, b, J if b else None)
request("P63", "POST", "/what-if", json.dumps({"what_if_context": "x", "scenario": {}}), J)
request("P64", "GET", "/account/consent-version")
request("P65", "GET", "/account/me", None, {"Authorization": "Bearer garbage"})
request("P66", "GET", "/account/me", None, {"Authorization": "Bearer " + "a" * 6000})
request("P67", "GET", "/account/me", None, {"Authorization": "Basic YWJjOmRlZg=="})
request("P68", "POST", "/account/portfolio", "{}", {**J, "Authorization": "Bearer garbage"})
# ---- is there a rate limit on /profile?  (a cheap invalid body, no scoring)
hits = []
for i in range(25):
    hits.append(request(f"R{i + 1:02d}", "POST", "/profile", "{}", J)["status"])
rows.append({"id": "RATE", "req": "25 x POST /profile {}", "status": sorted(set(hits)), "echo": [], "traceback": False,
             "missing_sec": [], "headers": {}, "ratelimit": {}, "body": f"statuses: {hits}"})
json.dump({"count": count, "rows": rows}, open(OUT, "w"), indent=1)
print("requests used:", count)
