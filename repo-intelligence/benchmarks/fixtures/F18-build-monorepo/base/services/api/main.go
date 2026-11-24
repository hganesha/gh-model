package main

import (
	"net/http"

	"example.com/shop/libs/db"
)

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /status", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) })
	mux.HandleFunc("POST /orders", db.CreateOrder)
	http.ListenAndServe(":8080", mux)
}
