package main

import (
	"fmt"
	"log"
	"net/http"
	"os"
	"time"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		switch r.URL.Path {
		case "/":
			fmt.Fprintln(w, "Hello from Go")
		case "/health":
			fmt.Fprintln(w, "ok")
		default:
			http.NotFound(w, r)
		}
	})
	server := &http.Server{
		Addr:              "0.0.0.0:" + port,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("Go sample listening on %s", server.Addr)
	log.Fatal(server.ListenAndServe())
}
