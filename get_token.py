"""
Run this ONCE locally to get your Spotify refresh_token.
After that you never need to run it again.

Usage:
  pip install requests
  python get_token.py
"""
import hashlib, os, secrets, webbrowser, urllib.parse, http.server, threading
import requests

CLIENT_ID     = input("Spotify Client ID: ").strip()
CLIENT_SECRET = input("Spotify Client Secret: ").strip()
REDIRECT_URI  = "http://127.0.0.1:8888/callback"
SCOPE         = "user-read-currently-playing user-read-playback-state"

# ── PKCE ──────────────────────────────────────────────────────────────────────
code_verifier  = secrets.token_urlsafe(64)
code_challenge = (
    __import__("base64")
    .urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
    .rstrip(b"=")
    .decode()
)

auth_url = (
    "https://accounts.spotify.com/authorize?"
    + urllib.parse.urlencode({
        "client_id":             CLIENT_ID,
        "response_type":         "code",
        "redirect_uri":          REDIRECT_URI,
        "scope":                 SCOPE,
        "code_challenge_method": "S256",
        "code_challenge":        code_challenge,
    })
)

auth_code: list[str] = []

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            auth_code.append(params["code"][0])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Got it! You can close this tab.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
    def log_message(self, *a): pass

server = http.server.HTTPServer(("localhost", 8888), Handler)
t = threading.Thread(target=server.handle_request)
t.start()

print("\nOpening browser for Spotify login…")
webbrowser.open(auth_url)
t.join(timeout=120)

if not auth_code:
    print("ERROR: no code received.")
    raise SystemExit(1)

# ── Exchange code for tokens ──────────────────────────────────────────────────
resp = requests.post(
    "https://accounts.spotify.com/api/token",
    data={
        "grant_type":    "authorization_code",
        "code":          auth_code[0],
        "redirect_uri":  REDIRECT_URI,
        "client_id":     CLIENT_ID,
        "code_verifier": code_verifier,
    },
    auth=(CLIENT_ID, CLIENT_SECRET),
)
resp.raise_for_status()
data = resp.json()

print("\n✅ SUCCESS! Copy these values to your environment:\n")
print(f"SPOTIFY_CLIENT_ID     = {CLIENT_ID}")
print(f"SPOTIFY_CLIENT_SECRET = {CLIENT_SECRET}")
print(f"SPOTIFY_REFRESH_TOKEN = {data['refresh_token']}")