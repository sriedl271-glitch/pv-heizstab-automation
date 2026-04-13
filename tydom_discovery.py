"""
TYDOM Discovery Script - Version 8
Liest alle Geraete, Endpunkte und aktuellen Zustaende aus TYDOM.
Ziel: Geraete-IDs und Befehls-Format fuer Heizstab-Steuerung ermitteln.
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import ssl
import urllib.request
import urllib.parse
import websockets


TYDOM_MAC   = "001A25067773"
TYDOM_WSS   = f"wss://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1"
TYDOM_HTTPS = f"https://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1"

DELTADORE_AUTH_URL  = (
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
        hdr = f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp}"'
    if cp.get("opaque"):
        hdr += f', opaque="{cp["opaque"]}"'
    return hdr


def oauth2_token(email, passwort):
    req = urllib.request.Request(DELTADORE_AUTH_URL, headers={"User-Agent": "TydomApp/4.17.41"})
    with urllib.request.urlopen(req, timeout=15) as r:
        token_endpoint = json.loads(r.read().decode())["token_endpoint"]
    post = urllib.parse.urlencode({
        "username": email, "password": passwort,
        "grant_type": "password", "client_id": DELTADORE_CLIENT_ID, "scope": DELTADORE_SCOPE,
    }).encode("utf-8")
    req2 = urllib.request.Request(token_endpoint, data=post, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "TydomApp/4.17.41"})
    with urllib.request.urlopen(req2, timeout=15) as r:
        return json.loads(r.read().decode())["access_token"]


def gateway_passwort(access_token):
    req = urllib.request.Request(
        DELTADORE_API_SITES + TYDOM_MAC,
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": "TydomApp/4.17.41"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    # Rekursiv nach 'password' suchen
    def suche(obj):
        if isinstance(obj, dict):
            if "password" in obj and isinstance(obj["password"], str):
                return obj["password"]
            for v in obj.values():
                r = suche(v)
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
    req = urllib.request.Request(TYDOM_HTTPS, headers={"User-Agent": "TydomApp/4.17.41"})
    try:
        urllib.request.urlopen(req, context=ssl_ctx, timeout=10)
    except urllib.error.HTTPError as e:
        www_auth = e.headers.get("WWW-Authenticate", "")
        if e.code == 401 and "Digest" in www_auth:
            return parse_www_authenticate(www_auth)
    return None


def http_anfrage(methode, pfad, body=""):
    laenge = len(body.encode("utf-8")) if body else 0
    return (
        f"{methode} {pfad} HTTP/1.1\r\n"
        f"Content-Length: {laenge}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n"
        f"Transac-Id: 0\r\n"
        f"\r\n{body}"
    )


async def tydom_lesen(gw_passwort, challenge):
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    uri_pfad = f"/mediation/client?mac={TYDOM_MAC}&appli=1"
    auth = berechne_digest(TYDOM_MAC, gw_passwort, challenge, uri_pfad)

    async with websockets.connect(
        TYDOM_WSS,
        additional_headers={"Authorization": auth, "User-Agent": "TydomApp/4.17.41"},
        ssl=ssl_ctx, open_timeout=15, ping_interval=None,
    ) as ws:
        print("Verbunden mit TYDOM.\n")

        # ── Geraete lesen ────────────────────────────────
        print("=" * 60)
        print("GET /devices/data – alle Geraete und Zustande")
        print("=" * 60)
        await ws.send(http_anfrage("GET", "/devices/data"))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        text = raw if isinstance(raw, str) else raw.decode()
        if "\r\n\r\n" in text:
            body = text.split("\r\n\r\n", 1)[1]
            try:
                geraete = json.loads(body)
                # Alle Geraete ausgeben
                for g in (geraete if isinstance(geraete, list) else [geraete]):
                    gid   = g.get("id", "?")
                    gname = g.get("name", "?")
                    gtype = g.get("type", "?")
                    print(f"\nGeraet: '{gname}'  id={gid}  type={gtype}")
                    for ep in g.get("endpoints", []):
                        eid   = ep.get("id", "?")
                        etype = ep.get("type", "?")
                        print(f"  Endpunkt id={eid}  type={etype}")
                        for dp in ep.get("cdata", []):
                            print(f"    {dp.get('name','?')} = {dp.get('value','?')}  (type: {dp.get('type','?')})")
            except json.JSONDecodeError:
                print(body[:500])
        else:
            print(text[:300])

        # ── Szenen / Szenarien lesen ──────────────────────
        print("\n" + "=" * 60)
        print("GET /scenarios/data – Szenen (fuer Ein/Aus-Schaltung)")
        print("=" * 60)
        await ws.send(http_anfrage("GET", "/scenarios/data"))
        try:
            raw2 = await asyncio.wait_for(ws.recv(), timeout=8)
            text2 = raw2 if isinstance(raw2, str) else raw2.decode()
            if "\r\n\r\n" in text2:
                body2 = text2.split("\r\n\r\n", 1)[1]
                try:
                    szenen = json.loads(body2)
                    for s in (szenen if isinstance(szenen, list) else [szenen]):
                        print(f"Szene: '{s.get('name','?')}'  id={s.get('id','?')}")
                except json.JSONDecodeError:
                    print(body2[:300])
        except asyncio.TimeoutError:
            print("(keine Szenen-Antwort)")

        print("\n" + "=" * 60)
        print("Alle Daten gelesen.")
        print("=" * 60)


async def main():
    email    = os.environ.get("TYDOM_EMAIL", "")
    passwort = os.environ.get("TYDOM_PASSWORD", "")
    if not email or not passwort:
        print("FEHLER: TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        return

    print("TYDOM Discovery v8 – Geraete und Endpunkte lesen")
    print(f"MAC: {TYDOM_MAC}")

    print("\nSchritt 1: OAuth2-Token...")
    token = oauth2_token(email, passwort)
    print("OK")

    print("Schritt 2: Gateway-Passwort...")
    gw_pw = gateway_passwort(token)
    print(f"OK ({gw_pw[:4]}***)")

    print("Schritt 3: Digest-Challenge...")
    challenge = digest_challenge()
    print(f"OK (realm={challenge.get('realm')})")

    print("Schritt 4: Geraete lesen...\n")
    await tydom_lesen(gw_pw, challenge)


if __name__ == "__main__":
    asyncio.run(main())
