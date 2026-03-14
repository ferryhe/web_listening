from fastapi import FastAPI
from web_listening.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Web Listening API",
        description="Monitor websites for changes, download documents, generate AI summaries",
        version="0.1.0",
    )
    app.include_router(router, prefix="/api/v1")
    return app


app = create_app()
