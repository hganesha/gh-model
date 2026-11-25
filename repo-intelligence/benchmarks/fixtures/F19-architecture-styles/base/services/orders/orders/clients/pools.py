from concurrent.futures import ThreadPoolExecutor

# One pool per downstream service, so a slow inventory service cannot take the threads payment calls need.
inventory_pool = ThreadPoolExecutor(max_workers=4)
payments_pool = ThreadPoolExecutor(max_workers=2)
