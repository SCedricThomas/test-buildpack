FROM golang:1.26-bookworm
WORKDIR /app
COPY go.mod main.go ./
ENV CGO_ENABLED=0
RUN go build -trimpath -o /app/server .
ENV PORT=8080
EXPOSE 8080
CMD ["/app/server"]
