"""Stands in for both Frigate's HTTP API and the ntfy server."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"MOCKSNAPSHOT" * 20 + b"\xff\xd9"
received = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/_received":
            body = json.dumps(received).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "/snapshot.jpg" in self.path:
            print(f"[mock] snapshot requested: {self.path}", flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(FAKE_JPEG)))
            self.end_headers()
            self.wfile.write(FAKE_JPEG)
            return
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        received.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body_len": len(body),
                "is_jpeg": body[:2] == b"\xff\xd8",
                "body_text": "" if body[:2] == b"\xff\xd8" else body.decode(errors="replace"),
            }
        )
        print(f"[mock] ntfy POST {self.path} ({len(body)} bytes)", flush=True)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


HTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
