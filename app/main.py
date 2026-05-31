import time
import uuid
import logging
import json
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.database import init_db, engine
from app.models import IngestRequest
from app.ingestion import ingest_events
from app.metrics import get_metrics, get_funnel, get_heatmap
from app.anomalies import get_anomalies
from app.health import get_health

# ── Structured logger ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","message":%(message)s}',
)
logger = logging.getLogger("store_intelligence")


# ── Startup / shutdown ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info('"Database initialised"')
    yield


app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Request logging middleware ─────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id = str(uuid.uuid4())[:8]
    start = time.time()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(json.dumps({
            "trace_id": trace_id,
            "endpoint": request.url.path,
            "error": str(exc),
        }))
        raise
    latency_ms = round((time.time() - start) * 1000, 2)
    store_id = request.path_params.get("store_id", "-")
    logger.info(json.dumps({
        "trace_id": trace_id,
        "store_id": store_id,
        "endpoint": request.url.path,
        "method": request.method,
        "status_code": response.status_code,
        "latency_ms": latency_ms,
    }))
    return response


# ── DB error handler ───────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(json.dumps({"error": str(exc), "path": request.url.path}))
    return JSONResponse(
        status_code=503,
        content={"error": "Service temporarily unavailable", "detail": str(exc)},
    )


# ── Helper: get a DB connection ────────────────────────────────────────────────
def get_conn():
    try:
        return engine.connect()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {e}")


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/events/ingest")
def ingest(request: IngestRequest):
    if len(request.events) > 500:
        raise HTTPException(status_code=400, detail="Batch size exceeds 500 events")

    with get_conn() as conn:
        result = ingest_events(request, conn)

    logger.info(json.dumps({
        "endpoint": "/events/ingest",
        "event_count": len(request.events),
        "accepted": result.accepted,
        "rejected": result.rejected,
    }))
    return result

@app.get("/stores/{store_id}/metrics")
def metrics(store_id: str):
    """
    Real-time store metrics:
    unique visitors, conversion rate, avg dwell per zone,
    queue depth, abandonment rate.
    """
    with get_conn() as conn:
        return get_metrics(store_id, conn)


@app.get("/stores/{store_id}/funnel")
def funnel(store_id: str):
    """
    Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase
    Session is the unit. Re-entries do not double-count.
    """
    with get_conn() as conn:
        return get_funnel(store_id, conn)


@app.get("/stores/{store_id}/heatmap")
def heatmap(store_id: str):
    """
    Zone visit frequency + avg dwell, normalised 0-100.
    Includes data_confidence flag if fewer than 20 sessions.
    """
    with get_conn() as conn:
        return get_heatmap(store_id, conn)


@app.get("/stores/{store_id}/anomalies")
def anomalies(store_id: str):
    """
    Active anomalies: queue spike, conversion drop, dead zone, stale feed.
    Severity: INFO / WARN / CRITICAL.
    """
    with get_conn() as conn:
        return get_anomalies(store_id, conn)


@app.get("/health")
def health():
    """
    Service status + last event timestamp per store.
    STALE_FEED warning if >10 min lag.
    """
    with get_conn() as conn:
        return get_health(conn)