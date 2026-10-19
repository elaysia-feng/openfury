"""The HTTP server: the /v1 platform API, its docs at /apidoc, and the dial/webhook loops.

    python -m openfury.server            # 127.0.0.1:8097; put a TLS proxy in front (deploy/Caddyfile)
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()
from openfury import api  # noqa: E402  (reads env at import)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await api.DISPATCHER.start()
    yield


app = FastAPI(title="openfury", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.include_router(api.router)
api.install_error_handlers(app)


def main() -> None:
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8097")),
                log_level="info")


if __name__ == "__main__":
    main()
