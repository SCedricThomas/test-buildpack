use std::env;
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream};
use std::time::Duration;

fn respond(mut stream: TcpStream) -> std::io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;
    let mut reader = BufReader::new(&stream);
    let mut request = String::new();
    // This intentionally small smoke-test server closes after each response.
    reader.read_line(&mut request)?;
    let (status, body) = match request.split_whitespace().nth(1) {
        Some("/") => ("200 OK", "Hello from Rust\n"),
        Some("/health") => ("200 OK", "ok\n"),
        _ => ("404 Not Found", "not found\n"),
    };
    write!(
        stream,
        "HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )?;
    stream.flush()
}

fn main() -> std::io::Result<()> {
    let port = env::var("PORT").unwrap_or_else(|_| "8080".into());
    let address = format!("0.0.0.0:{port}");
    let listener = TcpListener::bind(&address)?;
    eprintln!("Rust sample listening on {address}");
    for stream in listener.incoming() {
        match stream.and_then(respond) {
            Ok(()) => {}
            Err(error) => eprintln!("request failed: {error}"),
        }
    }
    Ok(())
}
