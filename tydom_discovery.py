"""
TYDOM Discovery Script - Version 12
Entscheidender Fix: Remote-Modus erfordert Praefix-Byte 0x02 vor jeder Nachricht!
Quelle: hass-deltadore-tydom-component, TydomClient._cmd_prefix = b"\x02" (remote)
Nachrichten werden als BYTES gesendet, nicht als Text.
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

# Remote-Modus: jede Nachricht beginnt mit diesem Byte
CMD_PREFIX = b"\x02"

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


def http_bytes(methode, pfad, body=""):
    """Erstellt eine HTTP-over-WebSocket Nachricht als Bytes mit 0x02 Praefix."""
    laenge = len(body.encode("utf-8")) if body else 0
    nachricht = (
        f"{methode} {pfad} HTTP/1.1\r\n"
        f"Content-Length: {laenge}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n"
        f"Transac-Id: {transac_id()}\r\n"
        f"\r\n{body}"
    )
    return CMD_PREFIX + nachricht.encode("ascii")


def parse_antwort(raw):
    """Parst eine eingehende TYDOM-Nachricht (mit moeglichem 0x02 Praefix)."""
    if isinstance(raw, bytes):
        # 0x02 Praefix entfernen falls vorhanden
        if raw and raw[0] == 0x02:
            raw = raw[1:]
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    return text


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

        # ── Initialisierung: alle Befehle mit 0x02 Praefix senden ────────
        befehle = [
            ("GET",  "/ping"),
            ("GET",  "/info"),
            ("GET",  "/groups/file"),
            ("POST", "/refresh/all"),
            ("GET",  "/configs/file"),
            ("GET",  "/devices/meta"),
            ("GET",  "/devices/data"),
            ("GET",  "/scenarios/file"),
        ]

        print("Sende Befehle (mit 0x02 Praefix als Bytes):")
        for methode, pfad in befehle:
            msg_bytes = http_bytes(methode, pfad)
            print(f"  -> {methode} {pfad}  ({len(msg_bytes)} Bytes, erstes Byte: 0x{msg_bytes[0]:02X})")
            await ws.send(msg_bytes)
            await asyncio.sleep(0.2)

        # ── 30 Sekunden abhoren ───────────────────────────────────────────
        print("\nHoere 30 Sekunden ab...\n")
        nachricht_nr = 0
        start = time.time()

        while time.time() - start < 30:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
                nachricht_nr += 1
                text = parse_antwort(raw)
                raw_bytes = raw if isinstance(raw, bytes) else raw.encode()

                print(f"--- Nachricht #{nachricht_nr} ({len(raw_bytes)} Bytes) ---")
                print(f"  Erstes Byte: 0x{raw_bytes[0]:02X}" if raw_bytes else "  (leer)")

                if "\r\n\r\n" in text:
                    header_block, body = text.split("\r\n\r\n", 1)
                    erste_zeile = header_block.split("\r\n")[0]
                    print(f"  Erste Zeile: {erste_zeile}")
                    body = body.strip()
                    if body:
                        try:
                            daten = json.loads(body)
                            print("  Body (JSON):")
                            print(json.dumps(daten, indent=2, ensure_ascii=False)[:800])
                        except json.JSONDecodeError:
                            print(f"  Body: {body[:200]}")
                    else:
                        print("  (kein Body)")
                else:
                    print(f"  RAW: {repr(text[:200])}")
                print()

            except (asyncio.TimeoutError, asyncio.CancelledError):
                verbleibend = int(30 - (time.time() - start))
                if verbleibend % 9 == 0:
                    print(f"  (warte... noch {verbleibend} Sek.)")

        print(f"\nFertig. {nachricht_nr} Nachrichten empfangen.")
        if nachricht_nr == 0:
            print("TYDOM antwortet immer noch nicht.")
            print("Naechster Schritt: Lokale Verbindung (192.168.178.24) pruefen.")


async def main():
    email    = os.environ.get("TYDOM_EMAIL", "")
    passwort = os.environ.get("TYDOM_PASSWORD", "")
    if not email or not passwort:
        print("FEHLER: TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        return

    print("TYDOM Discovery v12 – Remote-Modus mit 0x02-Praefix")
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
