"""
TYDOM Discovery Script - Version 5
Korrekte HTTP Digest Access Authentication:
- Username = MAC-Adresse (NICHT E-Mail!)
- Passwort = Klartext (kein Hash!)
- Zweistufiger Prozess: erst Challenge holen, dann Digest berechnen
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


def berechne_digest_header(username, password, realm, nonce, qop, uri, methode="GET"):
    """
    Berechnet den HTTP Digest Authorization Header.
    Fuer qop=auth wird nc und cnonce benoetigt.
    """
    ha1 = md5(f"{username}:{realm}:{password}")
    ha2 = md5(f"{methode}:{uri}")

    if qop and "auth" in qop:
        nc = "00000001"
        cnonce = secrets.token_hex(8)
        response = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        header = (
            f'Digest username="{username}", realm="{realm}", '
            f'nonce="{nonce}", uri="{uri}", qop={qop}, '
            f'nc={nc}, cnonce="{cnonce}", response="{response}"'
        )
    else:
        # Ohne qop (einfacheres Format)
        response = md5(f"{ha1}:{nonce}:{ha2}")
        header = (
            f'Digest username="{username}", realm="{realm}", '
            f'nonce="{nonce}", uri="{uri}", response="{response}"'
        )

    return header


def schritt1_challenge_holen(username, password):
    """
    Schritt 1: HTTP GET -> Server gibt 401 mit Digest-Challenge zurueck.
    Wir lesen realm, nonce, qop aus dem WWW-Authenticate Header.
    """
    print("\n" + "=" * 60)
    print("SCHRITT 1: Digest-Challenge vom Server holen")
    print(f"URL: {HTTPS_URL}")
    print(f"Username: {username}")
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
        print("UNERWARTETE 200-Antwort ohne Auth – das sollte nicht passieren.")
        return None
    except urllib.error.HTTPError as e:
        print(f"HTTP Status: {e.code}")
        www_auth = e.headers.get("WWW-Authenticate", "")
        print(f"WWW-Authenticate: {www_auth}")

        if e.code == 401 and www_auth.startswith("Digest"):
            # Parameter aus dem Header extrahieren
            params = {}
            for match in re.finditer(r'(\w+)="?([^",\s]+)"?', www_auth):
                params[match.group(1)] = match.group(2)

            realm  = params.get("realm", "")
            nonce  = params.get("nonce", "")
            qop    = params.get("qop", "")

            print(f"\nExtrahierte Challenge:")
            print(f"  realm  = {realm}")
            print(f"  nonce  = {nonce}")
            print(f"  qop    = {qop}")

            if realm and nonce:
                return {"realm": realm, "nonce": nonce, "qop": qop}
            else:
                print("FEHLER: realm oder nonce fehlen im Header.")
                return None
        elif e.code == 401:
            print("401, aber kein Digest-Header. Unbekannte Auth-Methode.")
            print(f"Alle Header: {dict(e.headers)}")
            return None
        else:
            print(f"Unerwarteter Status {e.code}")
            return None
    except Exception as e:
        print(f"Fehler: {type(e).__name__}: {e}")
        return None


async def schritt2_websocket_verbinden(username, password, challenge):
    """
    Schritt 2: WebSocket mit berechnetem Digest-Header verbinden.
    """
    print("\n" + "=" * 60)
    print("SCHRITT 2: WebSocket mit Digest Auth verbinden")
    print("=" * 60)

    uri_pfad = f"/mediation/client?mac={TYDOM_MAC}&appli=1"

    auth_header = berechne_digest_header(
        username=username,
        password=password,
        realm=challenge["realm"],
        nonce=challenge["nonce"],
        qop=challenge["qop"],
        uri=uri_pfad,
        methode="GET",
    )

    print(f"Authorization: {auth_header[:80]}...")

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

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
            print("\n>>> VERBINDUNG ERFOLGREICH! <<<")

            # Geraete abfragen
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
                        print(f"Geraete: {json.dumps(daten, ensure_ascii=False)[:500]}")
                    except json.JSONDecodeError:
                        print(f"Antwort (kein JSON): {body[:300]}")
                else:
                    print(f"Antwort: {text[:300]}")
            except asyncio.TimeoutError:
                print("Timeout beim Lesen – Verbindung steht aber!")

            return True

    except Exception as e:
        fehler = str(e)
        if "401" in fehler:
            print(f"-> 401 Unauthorized – Digest-Berechnung noch nicht korrekt")
            print(f"   Details: {fehler[:200]}")
        elif "400" in fehler:
            print(f"-> 400 Bad Request – Format-Problem")
        elif "403" in fehler:
            print(f"-> 403 Forbidden")
        else:
            print(f"-> {type(e).__name__}: {fehler[:200]}")
        return False


async def schritt3_fallback_mit_email(email, password, challenge):
    """
    Fallback: Digest Auth mit E-Mail als Username statt MAC.
    """
    print("\n" + "=" * 60)
    print("SCHRITT 3: Fallback – E-Mail als Username")
    print("=" * 60)

    uri_pfad = f"/mediation/client?mac={TYDOM_MAC}&appli=1"

    auth_header = berechne_digest_header(
        username=email,
        password=password,
        realm=challenge["realm"],
        nonce=challenge["nonce"],
        qop=challenge["qop"],
        uri=uri_pfad,
        methode="GET",
    )

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    print(f"Username: {email}")
    print(f"Authorization: {auth_header[:80]}...")

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
            print("\n>>> VERBINDUNG ERFOLGREICH! <<<")
            return True

    except Exception as e:
        fehler = str(e)
        if "401" in fehler:
            print(f"-> 401 – Auch mit E-Mail fehlgeschlagen")
        else:
            print(f"-> {type(e).__name__}: {fehler[:200]}")
        return False


async def main():
    email    = os.environ.get("TYDOM_EMAIL", "")
    passwort = os.environ.get("TYDOM_PASSWORD", "")

    if not email or not passwort:
        print("FEHLER: TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        return

    print("TYDOM Discovery v5 – Digest Auth")
    print(f"MAC:   {TYDOM_MAC}")
    print(f"Email: {email[:4]}***")

    # Schritt 1: Challenge holen
    challenge = schritt1_challenge_holen(TYDOM_MAC, passwort)

    if not challenge:
        print("\nKonnte keine Digest-Challenge erhalten.")
        print("Moegliche Ursachen:")
        print("  - Server nicht erreichbar")
        print("  - Server verwendet kein HTTP Digest (unbekanntes Format)")
        return

    # Schritt 2: WebSocket mit MAC als Username
    ok = await schritt2_websocket_verbinden(TYDOM_MAC, passwort, challenge)

    if not ok:
        # Schritt 3: Fallback mit E-Mail als Username
        ok = await schritt3_fallback_mit_email(email, passwort, challenge)

    print("\n" + "=" * 60)
    if ok:
        print("ERGEBNIS: VERBINDUNG ERFOLGREICH!")
        print("Naechster Schritt: TYDOM-Steuerung in main.py einbauen")
    else:
        print("ERGEBNIS: Alle Versuche fehlgeschlagen")
        print("")
        print("Bitte sende die Ausgabe von Schritt 1 (realm, nonce, qop)")
        print("damit wir das korrekte Auth-Format ermitteln koennen.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
