from fastapi import FastAPI
from web_listening import __version__
from web_listening.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Web Listening API",
        description="Monitor websites for changes, download documents, generate AI summaries",
        version=__version__,
    )
    app.include_router(router, prefix="/api/v1")
    return app


app = create_app()
