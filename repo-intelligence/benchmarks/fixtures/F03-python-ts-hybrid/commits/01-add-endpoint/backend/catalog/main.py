from fastapi import FastAPI

app = FastAPI()

PRODUCTS = [{"id": 1, "name": "Lamp"}]


@app.get("/products")
def list_products():
    return PRODUCTS


@app.get("/products/{product_id}")
def get_product(product_id: int):
    return next(p for p in PRODUCTS if p["id"] == product_id)
