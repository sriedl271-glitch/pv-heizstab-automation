"""
TYDOM Discovery Script - Version 6
Erweiterte Digest-Auth-Diagnose:
- Vollstaendiger WWW-Authenticate Header wird ausgegeben
- opaque-Feld wird erkannt und zurueckgesendet
- Mehrere URI-Varianten werden getestet
- Mehrere Username-Varianten werden getestet
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import ssl
import urllib.request
import websockets


TYDOM_MAC = "001A25067773"
TYDOM_URL = f"wss://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1"
HTTPS_URL = f"https://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1"


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def parse_digest_header(www_auth: str) -> dict:
    """Parst den WWW-Authenticate Digest Header robust."""
    params = {}
    # Variante mit Anfuehrungszeichen: key="value"
    for match in re.finditer(r'(\w+)="([^"]*)"', www_auth):
        params[match.group(1)] = match.group(2)
    # Variante ohne Anfuehrungszeichen: key=value
    for match in re.finditer(r'(\w+)=([^",\s]+)', www_auth):
        if match.group(1) not in params:
            params[match.group(1)] = match.group(2)
    return params


def berechne_digest_header(username, password, params, uri, methode="GET", algo="MD5"):
    """
    Berechnet den Authorization-Header fuer HTTP Digest Auth.
    Unterstuetzt MD5 und MD5-sess.
    """
    realm   = params.get("realm", "")
    nonce   = params.get("nonce", "")
    qop     = params.get("qop", "")
    opaque  = params.get("opaque", "")
    algorithm = params.get("algorithm", algo)

    nc     = "00000001"
    cnonce = secrets.token_hex(8)

    ha1 = md5(f"{username}:{realm}:{password}")
    if algorithm.upper() == "MD5-SESS":
        ha1 = md5(f"{ha1}:{nonce}:{cnonce}")

    ha2 = md5(f"{methode}:{uri}")

    if qop and "auth" in qop:
        response = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        header = (
            f'Digest username="{username}", realm="{realm}", '
            f'nonce="{nonce}", uri="{uri}", algorithm={algorithm}, '
            f'qop={qop}, nc={nc}, cnonce="{cnonce}", response="{response}"'
        )
    else:
        response = md5(f"{ha1}:{nonce}:{ha2}")
        header = (
            f'Digest username="{username}", realm="{realm}", '
            f'nonce="{nonce}", uri="{uri}", algorithm={algorithm}, '
            f'response="{response}"'
        )

    if opaque:
        header += f', opaque="{opaque}"'

    return header


def schritt1_challenge_holen():
    """Holt die Digest-Challenge vom TYDOM-Server."""
    print("\n" + "=" * 60)
    print("SCHRITT 1: Digest-Challenge vom Server holen")
    print(f"URL: {HTTPS_URL}")
    print("=" * 60)

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        HTTPS_URL,
        headers={"User-Agent": "TydomApp/4.17.41"},
    )

    try:
        urllib.request.urlopen(req, context=ssl_ctx, timeout=10)
        print("Unerwartete 200-Antwort – kein Auth erforderlich?")
        return None
    except urllib.error.HTTPError as e:
        print(f"HTTP Status: {e.code}")
        www_auth = e.headers.get("WWW-Authenticate", "")

        # Vollstaendigen Header ausgeben (wichtig fuer Diagnose)
        print(f"\nVollstaendiger WWW-Authenticate Header:")
        print(f"  {www_auth}")

        if e.code == 401 and "Digest" in www_auth:
            params = parse_digest_header(www_auth)
            print(f"\nExtrahierte Felder:")
            for k, v in params.items():
                if k == "nonce":
                    print(f"  {k} = {v[:20]}... (gekuerzt)")
                else:
                    print(f"  {k} = {v}")
            return params
        elif e.code == 401:
            print("\n401, aber kein Digest-Header!")
            print(f"Alle Headers: {dict(e.headers)}")
            return None
        else:
            print(f"Unerwarteter Status: {e.code}")
            return None
    except Exception as e:
        print(f"Fehler: {type(e).__name__}: {e}")
        return None


async def teste_verbindung(label, username, password, params, uri):
    """Testet eine einzelne Digest-Auth-Kombination per WebSocket."""
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    auth_header = berechne_digest_header(username, password, params, uri)

    print(f"\n--- {label} ---")
    print(f"  username = {username}")
    print(f"  uri      = {uri}")

    try:
        async with websockets.connect(
            TYDOM_URL,
            additional_headers={
                "Authorization": auth_header,
                "User-Agent": "TydomApp/4.17.41",
            },
            ssl=ssl_ctx,
            open_timeout=15,
            ping_interval=None,
        ) as ws:
            print("  >>> VERBINDUNG ERFOLGREICH! <<<")

            anfrage = (
                "GET /devices/data HTTP/1.1\r\n"
                "Content-Length: 0\r\n"
                "Content-Type: application/json; charset=UTF-8\r\n"
                "Transac-Id: 0\r\n"
                "\r\n"
            )
            await ws.send(anfrage)

            try:
                antwort = await asyncio.wait_for(ws.recv(), timeout=10)
                text = antwort if isinstance(antwort, str) else antwort.decode()
                if "\r\n\r\n" in text:
                    body = text.split("\r\n\r\n", 1)[1]
                    try:
                        daten = json.loads(body)
                        print(f"  Geraete: {json.dumps(daten, ensure_ascii=False)[:300]}")
                    except json.JSONDecodeError:
                        print(f"  Antwort: {body[:200]}")
                else:
                    print(f"  Antwort: {text[:200]}")
            except asyncio.TimeoutError:
                print("  Timeout beim Lesen – Verbindung steht aber!")
            return True

    except Exception as e:
        fehler = str(e)
        if "401" in fehler:
            print(f"  -> 401")
        elif "400" in fehler:
            print(f"  -> 400 Bad Request")
        elif "403" in fehler:
            print(f"  -> 403 Forbidden")
        else:
            print(f"  -> {type(e).__name__}: {fehler[:100]}")
        return False


async def schritt2_alle_kombinationen(email, passwort, params):
    """Testet systematisch alle sinnvollen Kombinationen."""
    print("\n" + "=" * 60)
    print("SCHRITT 2: Systematische Kombinationen testen")
    print("=" * 60)

    # URI-Varianten
    uri_mit_params  = f"/mediation/client?mac={TYDOM_MAC}&appli=1"
    uri_ohne_params = "/mediation/client"
    uri_voll        = f"https://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1"

    # Passwort-Varianten
    pw_plain = passwort
    pw_md5   = md5(passwort)

    kombinationen = [
        # (Label, username, password, uri)
        ("MAC + Klartext + URI mit Params",   TYDOM_MAC, pw_plain, uri_mit_params),
        ("MAC + Klartext + URI ohne Params",  TYDOM_MAC, pw_plain, uri_ohne_params),
        ("Email + Klartext + URI mit Params", email,     pw_plain, uri_mit_params),
        ("Email + Klartext + URI ohne Params",email,     pw_plain, uri_ohne_params),
        ("MAC + MD5-PW + URI mit Params",     TYDOM_MAC, pw_md5,   uri_mit_params),
        ("Email + MD5-PW + URI mit Params",   email,     pw_md5,   uri_mit_params),
        ("MAC + Klartext + volle URI",        TYDOM_MAC, pw_plain, uri_voll),
    ]

    for label, user, pw, uri in kombinationen:
        ok = await teste_verbindung(label, user, pw, params, uri)
        if ok:
            print(f"\n*** ERFOLG mit: {label} ***")
            return True
        await asyncio.sleep(0.3)

    return False


async def main():
    email    = os.environ.get("TYDOM_EMAIL", "")
    passwort = os.environ.get("TYDOM_PASSWORD", "")

    if not email or not passwort:
        print("FEHLER: TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        return

    print("TYDOM Discovery v6 – Erweiterte Digest-Auth-Diagnose")
    print(f"MAC:   {TYDOM_MAC}")
    print(f"Email: {email[:4]}***")

    # Challenge holen
    params = schritt1_challenge_holen()

    if not params:
        print("\nKonnte keine Digest-Challenge erhalten – Abbruch.")
        return

    # Alle Kombinationen testen
    ok = await schritt2_alle_kombinationen(email, passwort, params)

    print("\n" + "=" * 60)
    if ok:
        print("ERGEBNIS: VERBINDUNG ERFOLGREICH!")
    else:
        print("ERGEBNIS: Alle Kombinationen fehlgeschlagen.")
        print("")
        print("Wichtig: Bitte sende den vollstaendigen")
        print("WWW-Authenticate Header aus Schritt 1.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
