from concurrent.futures import ThreadPoolExecutor

# One pool for every downstream call.
pool = ThreadPoolExecutor(max_workers=6)
