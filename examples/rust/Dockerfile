FROM rust:1.99-slim-bookworm
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src/ ./src/
RUN cargo build --release --locked --offline
ENV PORT=8080
EXPOSE 8080
CMD ["/app/target/release/scalingo-dockerfile-sample"]
