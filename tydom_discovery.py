"""
TYDOM Discovery Script - Version 10
Korrekte Initialisierungsreihenfolge nach hass-deltadore-tydom-component:
1. GET /ping
2. GET /info
3. GET /groups/file
4. POST /refresh/all  <-- wichtig! erst danach kommen Geraete-Daten
5. GET /devices/data
6. GET /scenarios/file

Transac-Id = Unix-Zeitstempel in Millisekunden (nicht 0!)
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


def transac_id():
    """Unix-Zeitstempel in Millisekunden als Transac-Id."""
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
    realm, nonce, qop = cp.get("realm", ""), cp.get("nonce", ""), cp.get("qop", "")
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
    req = urllib.request.Request(
        DELTADORE_AUTH_URL, headers={"User-Agent": "TydomApp/4.17.41"})
    with urllib.request.urlopen(req, timeout=15) as r:
        token_endpoint = json.loads(r.read().decode())["token_endpoint"]
    post = urllib.parse.urlencode({
        "username": email, "password": passwort,
        "grant_type": "password", "client_id": DELTADORE_CLIENT_ID,
        "scope": DELTADORE_SCOPE,
    }).encode("utf-8")
    req2 = urllib.request.Request(
        token_endpoint, data=post, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "TydomApp/4.17.41"})
    with urllib.request.urlopen(req2, timeout=15) as r:
        return json.loads(r.read().decode())["access_token"]


def gateway_passwort(access_token):
    req = urllib.request.Request(
        DELTADORE_API_SITES + TYDOM_MAC,
        headers={"Authorization": f"Bearer {access_token}",
                 "User-Agent": "TydomApp/4.17.41"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    def suche(obj):
        if isinstance(obj, dict):
            if "password" in obj and isinstance(obj["password"], str):
                return obj["password"]
            for v in obj.values():
                r = suche(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = suche(item)
                if r:
                    return r
        return None
    return suche(data)


def digest_challenge():
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        TYDOM_HTTPS, headers={"User-Agent": "TydomApp/4.17.41"})
    try:
        urllib.request.urlopen(req, context=ssl_ctx, timeout=10)
    except urllib.error.HTTPError as e:
        www_auth = e.headers.get("WWW-Authenticate", "")
        if e.code == 401 and "Digest" in www_auth:
            return parse_www_authenticate(www_auth)
    return None


def http_anfrage(methode, pfad, body=""):
    """HTTP-over-WebSocket Anfrage mit korrekter Transac-Id."""
    laenge = len(body.encode("utf-8")) if body else 0
    return (
        f"{methode} {pfad} HTTP/1.1\r\n"
        f"Content-Length: {laenge}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n"
        f"Transac-Id: {transac_id()}\r\n"
        f"\r\n{body}"
    )


async def lese_antworten(ws, anzahl=3, timeout=8, label=""):
    """Liest bis zu 'anzahl' Nachrichten oder bis Timeout."""
    nachrichten = []
    for i in range(anzahl):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
            nachrichten.append(text)
            if "\r\n\r\n" in text:
                erste_zeile = text.split("\r\n")[0]
                body = text.split("\r\n\r\n", 1)[1].strip()
                print(f"  [{label}] <- {erste_zeile}")
                if body and len(body) > 2:
                    try:
                        daten = json.loads(body)
                        return daten
                    except json.JSONDecodeError:
                        print(f"  [{label}] body (kein JSON): {body[:100]}")
            else:
                print(f"  [{label}] <- (keine HTTP-Struktur): {text[:80]}")
        except (asyncio.TimeoutError, asyncio.CancelledError):
            break
    return None


async def tydom_erkunden(gw_pw, challenge):
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    uri_pfad = f"/mediation/client?mac={TYDOM_MAC}&appli=1"
    auth = berechne_digest(TYDOM_MAC, gw_pw, challenge, uri_pfad)

    async with websockets.connect(
        TYDOM_WSS,
        additional_headers={"Authorization": auth, "User-Agent": "TydomApp/4.17.41"},
        ssl=ssl_ctx, open_timeout=15, ping_interval=None,
    ) as ws:
        print("Verbunden!\n")

        # ── Initialisierungssequenz ───────────────────────────────────────
        print("=== INITIALISIERUNG ===")

        # 1. Ping
        print("\n1) GET /ping")
        await ws.send(http_anfrage("GET", "/ping"))
        await lese_antworten(ws, anzahl=2, timeout=5, label="ping")

        # 2. Info
        print("\n2) GET /info")
        await ws.send(http_anfrage("GET", "/info"))
        info = await lese_antworten(ws, anzahl=2, timeout=5, label="info")
        if info:
            print(f"   Info: {json.dumps(info, ensure_ascii=False)[:200]}")

        # 3. Groups
        print("\n3) GET /groups/file")
        await ws.send(http_anfrage("GET", "/groups/file"))
        await lese_antworten(ws, anzahl=2, timeout=5, label="groups")

        # 4. Refresh ALL (wichtig – triggert Geraete-Daten!)
        print("\n4) POST /refresh/all")
        await ws.send(http_anfrage("POST", "/refresh/all"))
        await lese_antworten(ws, anzahl=2, timeout=8, label="refresh")

        # 5. Configs
        print("\n5) GET /configs/file")
        await ws.send(http_anfrage("GET", "/configs/file"))
        await lese_antworten(ws, anzahl=2, timeout=5, label="configs")

        # ── Geraete lesen ─────────────────────────────────────────────────
        print("\n=== GERAETE ===")

        # 6. Devices Meta
        print("\n6) GET /devices/meta")
        await ws.send(http_anfrage("GET", "/devices/meta"))
        await lese_antworten(ws, anzahl=2, timeout=5, label="meta")

        # 7. Devices Data
        print("\n7) GET /devices/data")
        await ws.send(http_anfrage("GET", "/devices/data"))
        geraete = await lese_antworten(ws, anzahl=5, timeout=10, label="devices")

        if geraete:
            print("\n  GERAETE-LISTE:")
            geraete_liste = geraete if isinstance(geraete, list) else [geraete]
            for g in geraete_liste:
                gid   = g.get("id", "?")
                gname = g.get("name", "?")
                gtype = g.get("type", "?")
                print(f"\n  Geraet: '{gname}'  id={gid}  type={gtype}")
                for ep in g.get("endpoints", []):
                    eid   = ep.get("id", "?")
                    etype = ep.get("type", "?")
                    print(f"    Endpunkt: id={eid}  type={etype}")
                    for dp in ep.get("cdata", []):
                        name  = dp.get("name", "?")
                        value = dp.get("value", "?")
                        dtype = dp.get("type", "?")
                        print(f"      {name} = {value}  [{dtype}]")
        else:
            print("  Keine Geraete-Daten empfangen.")

        # ── Szenen lesen ──────────────────────────────────────────────────
        print("\n=== SZENEN ===")
        print("\n8) GET /scenarios/file")
        await ws.send(http_anfrage("GET", "/scenarios/file"))
        szenen = await lese_antworten(ws, anzahl=5, timeout=10, label="scenarios")

        if szenen:
            szenen_liste = szenen if isinstance(szenen, list) else [szenen]
            for s in szenen_liste:
                sid   = s.get("id", "?")
                sname = s.get("name", "?")
                print(f"  Szene: '{sname}'  id={sid}")
        else:
            print("  Keine Szenen empfangen.")

        print("\nFertig.")


async def main():
    email    = os.environ.get("TYDOM_EMAIL", "")
    passwort = os.environ.get("TYDOM_PASSWORD", "")
    if not email or not passwort:
        print("FEHLER: TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        return

    print("TYDOM Discovery v10 – Korrekte Initialisierung")
    print(f"MAC: {TYDOM_MAC}\n")

    print("OAuth2-Token...")
    token = oauth2_token(email, passwort)
    print("OK")

    print("Gateway-Passwort...")
    gw_pw = gateway_passwort(token)
    print(f"OK ({gw_pw[:4]}***)")

    print("Digest-Challenge...")
    challenge = digest_challenge()
    print(f"OK (realm={challenge.get('realm')})\n")

    await tydom_erkunden(gw_pw, challenge)


if __name__ == "__main__":
    asyncio.run(main())
