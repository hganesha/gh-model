from fastapi import FastAPI

app = FastAPI()

PRODUCTS = [{"id": 1, "name": "Lamp"}]


@app.get("/products")
def list_products():
    return PRODUCTS
