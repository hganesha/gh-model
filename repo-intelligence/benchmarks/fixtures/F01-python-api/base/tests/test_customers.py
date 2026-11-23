from app.main import create_customer, list_customers


def test_handlers_exist():
    assert callable(create_customer)
    assert callable(list_customers)
