from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(
        title="Image Processing Service",
        version="0.1.0",
        description="Upload images, transform them and retrieve them in different formats.",
    )

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
