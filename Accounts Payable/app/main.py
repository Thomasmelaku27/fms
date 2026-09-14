from fastapi import FastAPI

from app.api import accounts, journal
from app.api.errors import register_error_handlers
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.modules.registry import ROUTERS


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, debug=settings.debug)
    register_error_handlers(app)

    # The schema is managed by Alembic; run `alembic upgrade head` before starting.
    engine = make_engine(settings.database_url)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(accounts.router, prefix="/api/v1")
    app.include_router(journal.router, prefix="/api/v1")
    for router in ROUTERS:
        app.include_router(router, prefix="/api/v1")
    return app


app = create_app()
