#!/usr/bin/env python3
"""Build the sample sources, then check their HTTP responses using Docker."""

import argparse
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def docker(*args, capture=False):
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=capture)


def check(language):
    image = f"scalingo-dockerfile-sample-{language}:test"
    docker("build", "--tag", image, str(ROOT / "examples" / language))
    # Use a different PORT from the Dockerfile default to exercise Scalingo's
    # runtime port injection. Also verify that root is unnecessary at runtime.
    container = docker(
        "run", "--detach", "--user", "10000:10000", "--env", "PORT=9090",
        "--publish", "127.0.0.1::9090", image, capture=True,
    ).stdout.strip()
    try:
        address = docker("port", container, "9090/tcp", capture=True).stdout.strip()
        base = f"http://{address}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 45
        while True:
            try:
                with opener.open(base + "/health", timeout=2) as response:
                    assert response.status == 200
                    assert response.read() == b"ok\n"
                break
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
        with opener.open(base + "/", timeout=2) as response:
            assert response.status == 200
            assert response.read() == f"Hello from {language.title()}\n".encode()
        try:
            opener.open(base + "/missing", timeout=2)
        except urllib.error.HTTPError as error:
            assert error.code == 404
            error.close()
        else:
            raise AssertionError("expected HTTP 404")
        print(f"PASS {language}: source build, non-root runtime, PORT override, /, /health, 404", flush=True)
    finally:
        try:
            docker("logs", container)
        finally:
            docker("rm", "--force", container, capture=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("languages", nargs="*", choices=["go", "rust", "java"])
    args = parser.parse_args()
    for language in args.languages or ["go", "rust", "java"]:
        check(language)
