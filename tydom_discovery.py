"""
TYDOM Discovery Script - Version 13
Gezielt: Geraete-Namen, IDs und Szenarien uebersichtlich ausgeben.
Basiert auf v12 (0x02 Praefix funktioniert).
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import ssl
import time
import urllib.request
import urllib.parse
import websockets


TYDOM_MAC   = "001A25067773"
TYDOM_WSS   = f"wss://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1"
TYDOM_HTTPS = f"https://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1"
CMD_PREFIX  = b"\x02"

DELTADORE_AUTH_URL = (
    "https://deltadoreadb2ciot.b2clogin.com"
    "/deltadoreadb2ciot.onmicrosoft.com"
    "/v2.0/.well-known/openid-configuration"
    "?p=B2C_1_AccountProviderROPC_SignIn"
)
DELTADORE_CLIENT_ID = "8782839f-3264-472a-ab87-4d4e23524da4"
DELTADORE_SCOPE = (
    "openid profile offline_access "
    "https://deltadoreadb2ciot.onmicrosoft.com/iotapi/sites_management_allowed "
    "https://deltadoreadb2ciot.onmicrosoft.com/iotapi/sites_management_gateway_credentials "
    "https://deltadoreadb2ciot.onmicrosoft.com/iotapi/tydom_backend_allowed "
    "https://deltadoreadb2ciot.onmicrosoft.com/iotapi/websocket_remote_access"
)
DELTADORE_API_SITES = (
    "https://prod.iotdeltadore.com/sitesmanagement/api/v1/sites?gateway_mac="
)


def md5(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def transac_id():
    return str(time.time_ns() // 1_000_000)

def parse_www_authenticate(header):
    params = {}
    for m in re.finditer(r'(\w+)="([^"]*)"', header):
        params[m.group(1)] = m.group(2)
    for m in re.finditer(r'(\w+)=([^",\s]+)', header):
        if m.group(1) not in params:
            params[m.group(1)] = m.group(2)
    return params

def berechne_digest(username, password, cp, uri, methode="GET"):
    realm, nonce, qop = cp.get("realm",""), cp.get("nonce",""), cp.get("qop","")
    nc, cnonce = "00000001", secrets.token_hex(8)
    ha1 = md5(f"{username}:{realm}:{password}")
    ha2 = md5(f"{methode}:{uri}")
    if qop and "auth" in qop:
        resp = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        hdr = (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
               f'uri="{uri}", qop={qop}, nc={nc}, cnonce="{cnonce}", response="{resp}"')
    else:
        resp = md5(f"{ha1}:{nonce}:{ha2}")
        hdr = (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
               f'uri="{uri}", response="{resp}"')
    if cp.get("opaque"):
        hdr += f', opaque="{cp["opaque"]}"'
    return hdr

def oauth2_token(email, passwort):
    req = urllib.request.Request(DELTADORE_AUTH_URL, headers={"User-Agent":"TydomApp/4.17.41"})
    with urllib.request.urlopen(req, timeout=15) as r:
        token_endpoint = json.loads(r.read().decode())["token_endpoint"]
    post = urllib.parse.urlencode({
        "username": email, "password": passwort,
        "grant_type": "password", "client_id": DELTADORE_CLIENT_ID,
        "scope": DELTADORE_SCOPE,
    }).encode("utf-8")
    req2 = urllib.request.Request(token_endpoint, data=post, method="POST",
        headers={"Content-Type":"application/x-www-form-urlencoded","User-Agent":"TydomApp/4.17.41"})
    with urllib.request.urlopen(req2, timeout=15) as r:
        return json.loads(r.read().decode())["access_token"]

def gateway_passwort(access_token):
    req = urllib.request.Request(DELTADORE_API_SITES + TYDOM_MAC,
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": "TydomApp/4.17.41"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    def suche(obj):
        if isinstance(obj, dict):
            if "password" in obj and isinstance(obj["password"], str):
                return obj["password"]
            for v in obj.values():
                r = suche(v);
                if r: return r
        elif isinstance(obj, list):
            for item in obj:
                r = suche(item)
                if r: return r
        return None
    return suche(data)

def digest_challenge():
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(TYDOM_HTTPS, headers={"User-Agent":"TydomApp/4.17.41"})
    try:
        urllib.request.urlopen(req, context=ssl_ctx, timeout=10)
    except urllib.error.HTTPError as e:
        www_auth = e.headers.get("WWW-Authenticate","")
        if e.code == 401 and "Digest" in www_auth:
            return parse_www_authenticate(www_auth)
    return None

def sende(methode, pfad, body=""):
    laenge = len(body.encode("utf-8")) if body else 0
    msg = (f"{methode} {pfad} HTTP/1.1\r\n"
           f"Content-Length: {laenge}\r\nContent-Type: application/json; charset=UTF-8\r\n"
           f"Transac-Id: {transac_id()}\r\n\r\n{body}")
    return CMD_PREFIX + msg.encode("ascii")

def parse_body(raw):
    data = raw if isinstance(raw, bytes) else raw.encode()
    if data and data[0] == 0x02:
        data = data[1:]
    text = data.decode("utf-8", errors="replace")
    if "\r\n\r\n" in text:
        header, body = text.split("\r\n\r\n", 1)
        erste_zeile = header.split("\r\n")[0]
        try:
            return erste_zeile, json.loads(body.strip())
        except Exception:
            return erste_zeile, body.strip()
    return text[:60], None


async def tydom_erkunden(gw_pw, challenge):
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    uri_pfad = f"/mediation/client?mac={TYDOM_MAC}&appli=1"
    auth = berechne_digest(TYDOM_MAC, gw_pw, challenge, uri_pfad)

    async with websockets.connect(
        TYDOM_WSS,
        additional_headers={"Authorization": auth, "User-Agent": "TydomApp/4.17.41"},
        ssl=ssl_ctx, open_timeout=15, ping_interval=2,
    ) as ws:
        print("Verbunden!\n")

        # Alle Befehle senden
        for methode, pfad in [
            ("GET",  "/ping"),
            ("GET",  "/info"),
            ("GET",  "/groups/file"),
            ("POST", "/refresh/all"),
            ("GET",  "/configs/file"),
            ("GET",  "/devices/meta"),
            ("GET",  "/devices/data"),
            ("GET",  "/scenarios/file"),
        ]:
            await ws.send(sende(methode, pfad))
            await asyncio.sleep(0.2)

        # Alle Antworten sammeln
        alle_daten = {}
        start = time.time()
        nr = 0
        while time.time() - start < 20:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
                nr += 1
                erste_zeile, body = parse_body(raw)
                if body:
                    alle_daten[nr] = (erste_zeile, body)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if time.time() - start > 12:
                    break

        print(f"{nr} Nachrichten empfangen.\n")

        # ── VOLLSTAENDIGE JSON-AUSGABE aller Nachrichten ──────────────────
        for n, (zeile, body) in alle_daten.items():
            print(f"\n{'='*60}")
            print(f"Nachricht #{n}: {zeile}")
            print('='*60)
            print(json.dumps(body, indent=2, ensure_ascii=False))

        print("\n\nFERTIG.")


async def main():
    email    = os.environ.get("TYDOM_EMAIL", "")
    passwort = os.environ.get("TYDOM_PASSWORD", "")
    if not email or not passwort:
        print("FEHLER: TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        return

    print("TYDOM Discovery v13 – Vollstaendige Datenausgabe")
    print(f"MAC: {TYDOM_MAC}\n")

    print("OAuth2 + Gateway-Passwort + Challenge...")
    token    = oauth2_token(email, passwort)
    gw_pw    = gateway_passwort(token)
    challenge = digest_challenge()
    print(f"OK (realm={challenge.get('realm')})\n")

    await tydom_erkunden(gw_pw, challenge)


if __name__ == "__main__":
    asyncio.run(main())
