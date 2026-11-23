import os

from flask import Flask

app = Flask(__name__)


@app.get("/status")
def status():
    return {"db": bool(os.environ.get("DATABASE_URL"))}
