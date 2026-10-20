import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

# One math-library thread per process: the matrices here are small, so BLAS threads add nothing, while several portfolio builds at
# once would each start a full set (oversubscription), and on the Windows stack of the development PC concurrent native work hung
# or crashed (20 Oct, Damla and Onur). Set BEFORE numpy is imported; a value already in the environment wins.
for _threads in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_threads, "1")

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from chat import router as chat_router
from egoquant_routes import router as egoquant_router
from egoquant.anchoring.routes import router as anchoring_router
from egoquant.what_if_api import router as what_if_router

load_dotenv(Path(__file__).with_name(".env"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Agent C keeps its price cache warm in the background, whether or not traders use the app
    # (off with EGOQUANT_WARM=off, in synthetic mode, or without a Tiingo key).
    from egoquant.agents.market_intel.warm import start_background_warmer
    start_background_warmer()
    # Agent D rates the news in the background (off with EGOQUANT_NEWS_WORKER=off, without the AI tone switch,
    # in fixture/off news mode or with synthetic prices).
    from egoquant.briefing.worker import start_news_worker
    start_news_worker()
    yield


app = FastAPI(
    title="EgoQuant API",
    description="AI-powered Wealth Advisory System",
    version="0.1.0",
    lifespan=lifespan,
    redirect_slashes=False,       # /health/ is a 404, never a 307 to http:// (a POST redirected to http would resend the body in clear)
)

# The browser talks to the Next.js app, which calls this API server to server, so no browser origin needs
# direct access. The one listed (our Vercel site; override with CORS_ORIGINS, comma separated) is a
# convenience for the deployed pages; any other origin gets no CORS headers and no credentials.
_DEFAULT_ORIGINS = "https://fintechaton-frontend.vercel.app"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", _DEFAULT_ORIGINS).split(",") if o.strip()],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=False,
)

# Request-body size limit, checked on the Content-Length header BEFORE any body is read into memory (the Next.js
# proxy always sends a string body, so the header is always present). A body with no Content-Length (chunked) on
# a route that takes a body gets 411 instead of being read without a bound.
_MB = 1_000_000
_BODY_LIMITS = (                              # (path prefix, max bytes); first match wins, else the default
    (("/portfolio", "/trade-check", "/account", "/debug/portfolio", "/verify"), 4_500_000),   # answers + a 2 MB IBKR report
                                                                     # (JSON-escaped); a proof file carries the saved report
    (("/chat", "/briefing", "/what-if"), 512_000),
)
_DEFAULT_BODY_LIMIT = _MB


def _body_limit(path: str) -> int:
    for prefixes, limit in _BODY_LIMITS:
        if any(path == p or path.startswith(p + "/") for p in prefixes):
            return limit
    return _DEFAULT_BODY_LIMIT


class BodySizeLimit:
    """Pure ASGI middleware: 413 for a declared body over the limit, 411 for a body of unknown length."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            headers = {k.lower(): v for k, v in scope["headers"]}
            declared = headers.get(b"content-length")
            status = None
            if declared is None:
                status = 411 if b"transfer-encoding" in headers else None
            else:
                try:
                    status = 413 if int(declared) > _body_limit(scope["path"]) else None
                except ValueError:
                    status = 400
            if status is not None:
                message = {413: "Request is too large.", 411: "A Content-Length header is required.",
                           400: "Invalid Content-Length."}[status]
                await JSONResponse({"detail": message}, status_code=status)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(BodySizeLimit)             # added last = outermost: nothing else runs for an oversized request

_CHAT_LIMIT = 120
_CHAT_WINDOW = 60.0
_chat_timestamps: deque = deque()


@app.middleware("http")
async def chat_rate_limit(request: Request, call_next):
    if request.url.path in ("/chat", "/chat/stream") and request.method == "POST":
        now = time.monotonic()
        while _chat_timestamps and now - _chat_timestamps[0] > _CHAT_WINDOW:
            _chat_timestamps.popleft()
        if len(_chat_timestamps) >= _CHAT_LIMIT:
            return JSONResponse(
                {"detail": "Too many requests. Please try again later."},
                status_code=429, headers={"Retry-After": "60"},
            )
        _chat_timestamps.append(now)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"     # honoured by browsers over https only
    return response


def _clip(text: object, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# The fields a person types, in the words the screens use. A 422 about one of them names it in words and says the rule
# ("Horizon in years: must be above 0"); the limits come from our own schema (`ctx`), never from what the client sent.
_FIELD_LABELS = {
    "amount_invested": "Amount invested", "amount_needed": "Money needed back", "horizon_years": "Horizon in years",
    "goal_amount": "Goal amount", "monthly_flow": "Monthly amount", "liquidity_need": "Cash you may need soon",
    "min_cash_buffer": "Cash buffer", "tolerance": "Risk Game score", "loss_acceptance": "Risk Game loss score",
    "amount": "Amount", "percent_of_position": "Share of the position", "ticker": "Product ticker",
    "interest_areas": "Interest areas", "trading_platforms": "Trading platforms", "current_holdings": "Current holdings",
    "username": "Username", "name": "Name", "completed_at": "Time the game was played",
}


def _rule_text(error: dict) -> str | None:
    """The rule a value broke, in words, for the error types that name a number or a size; None for anything else."""
    kind, ctx = error.get("type", ""), error.get("ctx") or {}

    def num(key: str) -> str:
        value = ctx.get(key)
        return f"{value:g}" if isinstance(value, (int, float)) else str(value)

    rules = {
        "greater_than": lambda: f"must be above {num('gt')}",
        "greater_than_equal": lambda: f"must be {num('ge')} or more",
        "less_than": lambda: f"must be below {num('lt')}",
        "less_than_equal": lambda: f"must be {num('le')} or less",
        "missing": lambda: "is required",
        "string_too_long": lambda: f"is too long (at most {num('max_length')} characters)",
        "string_too_short": lambda: "is too short",
        "too_long": lambda: f"has too many entries (at most {num('max_length')})",
        "too_short": lambda: "needs at least one entry",
        "finite_number": lambda: "must be a finite number",
        "int_parsing": lambda: "must be a whole number",
        "float_parsing": lambda: "must be a number",
        "int_type": lambda: "must be a whole number",
        "float_type": lambda: "must be a number",
        "bool_parsing": lambda: "must be yes or no",
        "datetime_parsing": lambda: "must be a date and time",
    }
    rule = rules.get(kind)
    try:
        return rule() if rule else None
    except Exception:
        return None


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    """A 422 says which field is wrong and why, never what was sent. FastAPI's default body repeats each
    offending value (`input`) and the validator's context (`ctx`), which would send a credential such as an
    IBKR Flex token or a sealed chat context straight back to whoever posted it (and into any proxy log)."""
    def _loc(error: dict) -> list:
        parts = [_clip(part, 40) if isinstance(part, str) else part for part in list(error.get("loc", ()))[:8]]
        if error.get("type") == "extra_forbidden" and parts:     # the name of a field we do not have was typed by the client
            parts[-1] = "(field not allowed)"
        return parts

    def _item(error: dict) -> dict:
        loc, msg = _loc(error), str(error.get("msg", "Invalid value")).removeprefix("Value error, ")
        names = [p for p in loc if isinstance(p, str)]
        label = _FIELD_LABELS.get(names[-1]) if names and error.get("type") != "extra_forbidden" else None
        rule = _rule_text(error) if label else None
        if label and rule:                                        # "Horizon in years: must be above 0"
            loc, msg = ["body", label], rule
        return {"type": _clip(error.get("type", "invalid"), 60), "loc": loc, "msg": _clip(msg, 300)}

    return JSONResponse({"detail": [_item(error) for error in exc.errors()[:20]]}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    """Anything unplanned becomes one generic 500: no stack trace, no exception text (which could carry a
    value the client sent). Only the exception type and the route are logged."""
    logging.getLogger("egoquant").error("Unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
    return JSONResponse({"detail": "Something went wrong on our side. Please retry."}, status_code=500)


app.include_router(chat_router)
app.include_router(egoquant_router)
app.include_router(what_if_router)
app.include_router(anchoring_router)      # on-chain sealing (egoquant/anchoring): off unless EGOQ_SEAL_ENABLED=1 and a key


@app.get("/")
def home():
    return {
        "project": "EgoQuant",
        "status": "running",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}
