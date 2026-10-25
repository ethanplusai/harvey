"""A localhost stand-in for Google Gemini REST API v1beta.

Used in tests to test GeminiClient and Brain(provider="gemini") with
zero external network calls and zero credentials.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeGeminiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        api_key = query.get("key", [""])[0]

        entry = {
            "method": "GET",
            "path": parsed.path,
            "api_key": api_key,
        }
        self.server.requests.append(entry)

        if self.server.scenario_queue:
            scenario = self.server.scenario_queue.pop(0)
        else:
            scenario = self.server.scenario

        if api_key == "INVALID_KEY" or scenario == "auth_error":
            return self._send(
                400,
                {
                    "error": {
                        "code": 400,
                        "message": "API key not valid. Please pass a valid API key.",
                        "status": "INVALID_ARGUMENT",
                    }
                },
            )

        if api_key == "FORBIDDEN" or scenario == "forbidden":
            return self._send(
                403,
                {"error": {"code": 403, "message": "Permission denied", "status": "PERMISSION_DENIED"}},
            )

        if scenario == "rate_limit" or api_key == "RATE_LIMIT":
            return self._send(
                429,
                {
                    "error": {
                        "code": 429,
                        "message": "Resource has been exhausted (e.g. check quota).",
                        "status": "RESOURCE_EXHAUSTED",
                    }
                },
            )

        if scenario == "server_error":
            return self._send(
                503,
                {
                    "error": {
                        "code": 503,
                        "message": "The service is temporarily unavailable.",
                        "status": "UNAVAILABLE",
                    }
                },
            )

        model_name = (
            parsed.path.split("/models/")[-1]
            if "/models/" in parsed.path
            else "gemini-2.5-flash"
        )
        return self._send(
            200,
            {
                "name": f"models/{model_name}",
                "displayName": "Fake Gemini",
                "supportedGenerationMethods": ["generateContent"],
            },
        )

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            body = raw.decode("utf-8", "replace")

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        api_key = query.get("key", [""])[0]

        entry = {
            "method": "POST",
            "path": parsed.path,
            "api_key": api_key,
            "body": body,
        }
        self.server.requests.append(entry)

        if self.server.scenario_queue:
            scenario = self.server.scenario_queue.pop(0)
        else:
            scenario = self.server.scenario

        if isinstance(body, dict) and "tools" in body and not body["tools"]:
            return self._send(
                400,
                {
                    "error": {
                        "code": 400,
                        "message": "Invalid value at 'tools': should not be empty.",
                        "status": "INVALID_ARGUMENT",
                    }
                },
            )

        if api_key == "INVALID_KEY" or scenario == "auth_error":
            return self._send(
                400,
                {
                    "error": {
                        "code": 400,
                        "message": "API key not valid. Please pass a valid API key.",
                        "status": "INVALID_ARGUMENT",
                    }
                },
            )

        if scenario == "rate_limit" or api_key == "RATE_LIMIT":
            return self._send(
                429,
                {
                    "error": {
                        "code": 429,
                        "message": "Resource has been exhausted (e.g. check quota).",
                        "status": "RESOURCE_EXHAUSTED",
                    }
                },
            )

        if scenario == "server_error":
            return self._send(
                503,
                {
                    "error": {
                        "code": 503,
                        "message": "The service is temporarily unavailable.",
                        "status": "UNAVAILABLE",
                    }
                },
            )

        if scenario == "blocked_prompt":
            return self._send(
                200,
                {
                    "candidates": [],
                    "promptFeedback": {"blockReason": "SAFETY"},
                },
            )

        if scenario == "stopped_candidate":
            return self._send(
                200,
                {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": []},
                            "finishReason": "SAFETY",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 12,
                        "candidatesTokenCount": 0,
                    },
                },
            )

        reply_text = self.server.custom_reply or "FAKE_GEMINI_REPLY"
        model = (
            parsed.path.split("/models/")[-1].split(":")[0]
            if "/models/" in parsed.path
            else "gemini-2.5-flash"
        )
        return self._send(
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": reply_text}],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 12,
                    "candidatesTokenCount": 8,
                    "totalTokenCount": 20,
                },
                "modelVersion": model,
            },
        )


class FakeGeminiServer(ThreadingHTTPServer):
    def __init__(self, host="127.0.0.1", port=0):
        super().__init__((host, port), FakeGeminiHandler)
        self.requests = []
        self.scenario = "normal"
        self.scenario_queue = []
        self.custom_reply = None
        self._thread = None

    def start(self) -> str:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return f"http://{self.server_address[0]}:{self.server_address[1]}"

    def stop(self):
        self.shutdown()
        self.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)


def main():
    if len(sys.argv) > 2:
        log_file, port_file = sys.argv[1], sys.argv[2]
        server = FakeGeminiServer()
        url = server.start()
        with open(port_file, "w") as f:
            f.write(str(server.server_address[1]))
        server.serve_forever()


if __name__ == "__main__":
    main()
