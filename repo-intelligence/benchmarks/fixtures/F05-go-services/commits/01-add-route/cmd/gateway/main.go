package main

import (
	"net/http"

	"github.com/go-chi/chi/v5"
)

func main() {
	r := chi.NewRouter()
	r.Get("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(200) })
	r.Get("/orders", func(w http.ResponseWriter, _ *http.Request) { w.Write([]byte("[]")) })
	http.ListenAndServe(":8080", r)
}
