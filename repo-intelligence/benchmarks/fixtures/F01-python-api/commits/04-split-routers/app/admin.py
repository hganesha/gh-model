from fastapi import FastAPI

admin = FastAPI()


@admin.get("/stats")
def stats():
    return {"customers": 0}
