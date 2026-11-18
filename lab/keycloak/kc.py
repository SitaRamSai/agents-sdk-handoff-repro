"""Small helpers shared by the lab's Keycloak scripts. Standard library only.

Never prints raw tokens: callers use decode() and print claims.
"""

import base64
import html
import http.cookiejar as cookiejar
import json
import os
import re
import secrets as pysecrets
import urllib.error
import urllib.parse
import urllib.request

KC_URL = os.environ.get("KC_URL", "http://localhost:8080").rstrip("/")
REALM = os.environ.get("KC_REALM", "lab")
HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(HERE, "secrets", "lab-clients.json")
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class KeycloakError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status, self.body = status, body


def http(method, url, data=None, token=None, form=False):
    headers = {"accept": "application/json"}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            headers["content-type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["content-type"] = "application/json"
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            location = resp.headers.get("location")
    except urllib.error.HTTPError as exc:
        raise KeycloakError(exc.code, exc.read().decode()) from None
    if not raw:
        return {"_location": location}
    return json.loads(raw)


def token_endpoint(realm=REALM):
    return f"{KC_URL}/realms/{realm}/protocol/openid-connect/token"


def jwks_url(realm=REALM):
    return f"{KC_URL}/realms/{realm}/protocol/openid-connect/certs"


def decode(token):
    """Return the JWT payload as a dict (no signature check: display only)."""
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def load_secrets():
    with open(SECRETS_FILE) as f:
        return json.load(f)


def client_credentials(client_id, secrets, scope=None):
    data = {"grant_type": "client_credentials", "client_id": client_id,
            "client_secret": secrets["clients"][client_id]}
    if scope:
        data["scope"] = scope
    return http("POST", token_endpoint(), data, form=True)["access_token"]


REDIRECT = "http://127.0.0.1:9999/callback"


class _Stop(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl.startswith(REDIRECT):
            raise _Arrived(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Arrived(Exception):
    def __init__(self, url):
        self.url = url


def _form(page):
    m = re.search(r'<form[^>]*action="([^"]+)"', page)
    if not m:
        raise RuntimeError("no form found on Keycloak page")
    action = urllib.parse.urljoin(KC_URL + "/", html.unescape(m.group(1)))
    hidden = dict(re.findall(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page))
    return action, hidden


def user_token(secrets, scope="openid files:read files:write",
               client_id="agent-app", approve_consent=True):
    """Sign alice in at `client_id` with the authorization code flow and return
    the token response (dict). Fills in Keycloak's own login and consent forms
    for the synthetic lab user only."""
    jar = cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _Stop())
    state = pysecrets.token_urlsafe(8)
    auth = (f"{KC_URL}/realms/{REALM}/protocol/openid-connect/auth?" +
            urllib.parse.urlencode({"client_id": client_id, "response_type": "code",
                                    "redirect_uri": REDIRECT, "scope": scope,
                                    "state": state}))
    try:
        page = opener.open(auth).read().decode()
        for c in jar:  # Keycloak marks cookies Secure; browsers allow that on
            c.secure = False  # localhost, Python's cookie jar does not
        action, hidden = _form(page)
        data = dict(hidden, username="alice", password=secrets["alice_password"])
        page = opener.open(action, urllib.parse.urlencode(data).encode()).read().decode()
        if "kc-form-login" in page or "Invalid username" in page:
            raise RuntimeError("Keycloak rejected alice's sign-in")
        # Consent screen (login-oauth-grant): approve or cancel.
        action, hidden = _form(page)
        data = dict(hidden, **({"accept": "Yes"} if approve_consent else {"cancel": "No"}))
        opener.open(action, urllib.parse.urlencode(data).encode())
        raise RuntimeError("Keycloak did not redirect back after consent")
    except _Arrived as done:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(done.url).query)
    if "code" not in q:
        raise RuntimeError(f"no code returned: {q.get('error')} {q.get('error_description')}")
    return http("POST", token_endpoint(), {
        "grant_type": "authorization_code", "code": q["code"][0],
        "redirect_uri": REDIRECT, "client_id": client_id,
        "client_secret": secrets["clients"][client_id]}, form=True)


def exchange(requester, secrets, subject_token, audience=None, scope=None,
             actor_token=None):
    """RFC 8693 token exchange as `requester` (a confidential lab client)."""
    data = {"grant_type": TOKEN_EXCHANGE, "client_id": requester,
            "client_secret": secrets["clients"][requester],
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE}
    if audience:
        data["audience"] = audience
    if scope:
        data["scope"] = scope
    if actor_token:
        data["actor_token"] = actor_token
        data["actor_token_type"] = ACCESS_TOKEN_TYPE
    return http("POST", token_endpoint(), data, form=True)["access_token"]
