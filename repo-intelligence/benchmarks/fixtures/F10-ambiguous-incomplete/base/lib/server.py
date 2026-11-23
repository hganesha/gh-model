import http.server

# Lives under lib/ but starts a server when run directly.
if __name__ == "__main__":
    http.server.HTTPServer(("", 8000), http.server.SimpleHTTPRequestHandler).serve_forever()
