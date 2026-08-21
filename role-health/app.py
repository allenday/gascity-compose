import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen

API = os.environ.get("CITY_API", "http://city:8372").rstrip("/")
CITY = os.environ.get("CITY_NAME", "my-city")
HOST = os.environ.get("CITY_API_HOST")


def role_state(role):
    url = f"{API}/v0/city/{CITY}/status"
    try:
        headers = {} if not HOST else {"Host": HOST}
        with urlopen(Request(url, headers=headers), timeout=5) as response:
            payload = json.load(response)
    except (URLError, ValueError, OSError) as exc:
        return False, {"state": "unavailable", "reason": f"city API unavailable: {exc}"}
    for agent in payload.get("agent_details", []):
        names = {agent.get("name"), agent.get("qualified_name")}
        if role in names or any(str(name).endswith("." + role) for name in names if name):
            if agent.get("suspended"):
                # A deliberately suspended role is not an operational failure.  Returning
                # success lets Gatus reserve red for roles that are genuinely unavailable.
                return True, {"state": "suspended", "reason": "role deliberately suspended", "agent": agent}
            return True, {"state": "active", "agent": agent}
    return False, {"state": "missing", "reason": "role not found"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            healthy, body = True, {"status": "ok"}
        elif self.path.startswith("/health/"):
            healthy, body = role_state(self.path.rsplit("/", 1)[-1])
        else:
            self.send_error(404)
            return
        encoded = json.dumps({"healthy": healthy, **body}).encode()
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        pass


HTTPServer(("0.0.0.0", 8081), Handler).serve_forever()
