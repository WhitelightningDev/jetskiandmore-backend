from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import asyncio
from .config import settings
from .routers import router as api_router
from .marketing_advisor import marketing_advisor_loop


app = FastAPI(title="JetSki & More API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True}


@app.on_event("startup")
async def _startup():
    # Fire-and-forget background marketing advisor loop (in-process).
    asyncio.create_task(marketing_advisor_loop())


app.include_router(api_router)
