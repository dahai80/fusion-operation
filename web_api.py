#!/usr/bin/env python3
"""Issue #24: REST surface for the douyin ops DAG pipeline.

Thin FastAPI layer over existing capabilities — no business logic here:
- graph execution delegates to run_graph.run_once (agent-studio daemon RPC first,
  local fallback), same path as cron and CLI
- queue/scheduler/health read from queue_store / daemon RPC / douyin_tools.config

In-memory run registry: POST /api/run starts a background asyncio task and
returns run_id; GET /api/runs/{id} polls status. Long graphs (5-90min) must
never block the HTTP response.

Auth: bearer token equals the configured mlx client key (issue #22 chain).
If no key is configured (anonymous dev), writes stay open but log a warning;
reads are always open. Bind to 127.0.0.1 by default (port-registry: fusion-operation).
"""
import argparse
import asyncio
import logging
import os
import time
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from douyin_tools.config import _mlx_api_key, mlx_health

logger = logging.getLogger("web_api")

app = FastAPI(title="fusion-operation", version="0.1.3")

# In-memory run registry (single-process service; daemon holds durable state).
_runs = {}

_API_PORT_DEFAULT = 11431


def _check_auth(authorization):
    # Bearer token must match the configured client key when one exists.
    # Anonymous dev (no key anywhere): allow, warn once — local-only bind.
    expected = _mlx_api_key()
    if not expected:
        logger.warning("no client key configured, write endpoints open (anonymous dev, 127.0.0.1 bind)")
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid bearer token")


class RunRequest(BaseModel):
    graph: str
    approve: bool = False
    vars: dict = {}


@app.get("/api/health")
async def health():
    from douyin_tools.config import AGENT_STUDIO_SOCKET
    from douyin_tools.rpc_auth import authed_call

    mlx = mlx_health()
    try:
        res = await asyncio.wait_for(authed_call("cron.list", {}, AGENT_STUDIO_SOCKET), timeout=15)
        daemon = {"ok": not res.get("error"), "job_count": len(res.get("jobs", [])), "error": res.get("error", "")}
    except Exception as e:
        daemon = {"ok": False, "job_count": 0, "error": str(e)}
    from douyin_tools.queue_ops import _get_store

    try:
        counts = _get_store().count()
    except Exception as e:
        logger.error("queue read failed: %s", e)
        counts = {}
    return {"mlx": mlx, "daemon": daemon, "queue": counts, "ts": int(time.time())}


@app.get("/api/queue")
async def queue():
    from douyin_tools.queue_ops import _get_store

    store = _get_store()
    counts = store.count()
    return {"counts": counts, "pending": store.list_by_status("pending"), "inflight": store.list_by_status("inflight")}


@app.get("/api/scheduler")
async def scheduler():
    from douyin_tools.config import AGENT_STUDIO_SOCKET
    from douyin_tools.rpc_auth import authed_call

    try:
        res = await asyncio.wait_for(authed_call("cron.list", {}, AGENT_STUDIO_SOCKET), timeout=15)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="daemon cron.list timed out >15s")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"daemon unreachable: {e}")
    if res.get("error"):
        raise HTTPException(status_code=502, detail=f"cron.list failed: {res['error']}")
    return {"jobs": res.get("jobs", [])}


@app.get("/api/graphs")
async def graphs():
    # Local graph templates; actual execution ids are resolved by run_graph cache.
    import glob
    import json as _json

    root = os.path.dirname(os.path.abspath(__file__))
    out = []
    for p in sorted(glob.glob(os.path.join(root, "graphs", "*.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                data = _json.load(f)
            out.append({"file": os.path.basename(p), "name": data.get("name", ""), "nodes": len(data.get("nodes", []))})
        except (OSError, ValueError) as e:
            logger.warning("graph %s unreadable: %s", p, e)
    return {"graphs": out}


async def _run_task(run_id, graph_path, approve, extra_vars):
    entry = _runs[run_id]
    try:
        from run_graph import run_once

        entry["status"] = "running"
        rc, result = await run_once(graph_path, approve, extra_vars)
        entry["status"] = "completed" if rc == 0 else "failed"
        entry["rc"] = rc
        entry["result"] = result[:4000]
    except Exception as e:
        logger.error("run %s failed: %s", run_id, e)
        entry["status"] = "failed"
        entry["rc"] = 1
        entry["result"] = str(e)
    entry["finished_at"] = int(time.time())


@app.post("/api/run")
async def run(req: RunRequest, authorization: str = Header(default="")):
    _check_auth(authorization)
    root = os.path.dirname(os.path.abspath(__file__))
    graph_path = os.path.join(root, "graphs", os.path.basename(req.graph))
    if not graph_path.endswith(".json") or not os.path.exists(graph_path):
        raise HTTPException(status_code=404, detail=f"graph not found: {req.graph}")
    run_id = uuid.uuid4().hex[:12]
    _runs[run_id] = {
        "status": "queued",
        "graph": os.path.basename(req.graph),
        "approve": req.approve,
        "vars": req.vars,
        "started_at": int(time.time()),
        "finished_at": None,
        "rc": None,
        "result": "",
    }
    asyncio.create_task(_run_task(run_id, graph_path, req.approve, req.vars))
    logger.info("run %s queued graph=%s approve=%s", run_id, req.graph, req.approve)
    return {"run_id": run_id, "status": "queued"}


@app.get("/api/runs")
async def runs():
    return {"runs": _runs}


@app.get("/api/runs/{run_id}")
async def run_status(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="run not found")
    return _runs[run_id]


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="fusion-operation REST API (issue #24)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FUSION_OPERATION_API_PORT", _API_PORT_DEFAULT)))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
