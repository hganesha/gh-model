package db

import (
	"database/sql"
	"net/http"
)

var conn *sql.DB

func CreateOrder(w http.ResponseWriter, r *http.Request) {
	conn.Exec("INSERT INTO orders (status) VALUES ('new')")
}
