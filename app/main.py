from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.routers import documents, generations, nodes, selections
from app.services.errors import ServiceError

app = FastAPI(title="CT-200 Document Intelligence API", version="1.0.0",
              description="Parse a technical manual into a versioned tree, select sections, generate QA test ideas, "
                          "and see at retrieval time whether they still match the current document.")


@app.exception_handler(ServiceError)
async def service_error_handler(_: Request, exc: ServiceError):
    return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": exc.message, **exc.extra})


for r in (documents.router, nodes.router, selections.router, generations.router):
    app.include_router(r)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}