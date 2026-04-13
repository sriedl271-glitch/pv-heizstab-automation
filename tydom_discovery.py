"""
TYDOM Discovery Script - Version 4
Analysiert zuerst was der TYDOM-Server wirklich verlangt,
dann versucht die passende Authentifizierung.
"""
import asyncio
import base64
import hashlib
import json
import os
import ssl
import requests
import websockets


TYDOM_MAC = "001A25067773"

# Verschiedene URL-Varianten zum Testen
URLS = [
    f"wss://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1",
    f"wss://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=2",
    f"wss://medi.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1",
    f"wss://tydom.deltadore.fr/mediation/client?mac={TYDOM_MAC}&appli=1",
]


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def basic_auth(user: str, passwort: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{passwort}".encode()).decode()


def berechne_digest_antwort(user, passwort, realm, nonce, methode, uri):
    ha1 = md5(f"{user}:{realm}:{passwort}")
    ha2 = md5(f"{methode}:{uri}")
    return md5(f"{ha1}:{nonce}:{ha2}")


def erstelle_http_anfrage(methode, pfad, body=""):
    laenge = len(body.encode("utf-8")) if body else 0
    return (
        f"{methode} {pfad} HTTP/1.1\r\n"
        f"Content-Length: {laenge}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n"
        f"Transac-Id: 0\r\n"
        f"\r\n{body}"
    )


def schritt1_https_probe(email, passwort):
    """
    Testet via HTTPS was der Server als Authentifizierung erwartet.
    """
    print("\n" + "="*60)
    print("SCHRITT 1: HTTPS-Probe (Auth-Typ ermitteln)")
    print("="*60)

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    test_urls = [
        "https://mediation.tydom.com/",
        "https://mediation.tydom.com/mediation/",
        "https://medi.tydom.com/",
    ]

    for url in test_urls:
        try:
            r = requests.get(url, timeout=10, verify=False,
                           headers={"User-Agent": "TydomApp/4.17.41"})
            print(f"\n{url}")
            print(f"  Status: {r.status_code}")
            print(f"  Headers: {dict(r.headers)}")
            if r.text:
                print(f"  Body: {r.text[:200]}")
        except requests.exceptions.SSLError as e:
            print(f"\n{url} -> SSL-Fehler: {e}")
        except requests.exceptions.ConnectionError as e:
            print(f"\n{url} -> Verbindungsfehler: {e}")
        except Exception as e:
            print(f"\n{url} -> {type(e).__name__}: {e}")

    # Mit Basic Auth probieren (HTTP-Ebene)
    print("\n--- HTTPS mit Basic Auth (MD5) ---")
    for url in ["https://mediation.tydom.com/mediation/",
                "https://medi.tydom.com/mediation/"]:
        try:
            pw_md5 = md5(passwort)
            r = requests.get(
                url, timeout=10, verify=False,
                headers={
                    "Authorization": basic_auth(email, pw_md5),
                    "x-ssl-client-dn": f"emailAddress={email}",
                    "User-Agent": "TydomApp/4.17.41",
                }
            )
            print(f"{url}")
            print(f"  Status: {r.status_code}")
            www_auth = r.headers.get("WWW-Authenticate", "")
            if www_auth:
                print(f"  WWW-Authenticate: {www_auth}")
        except Exception as e:
            print(f"{url} -> {type(e).__name__}: {e}")


async def schritt2_websocket_versuche(email, passwort):
    """
    Versucht verschiedene WebSocket-Authentifizierungen.
    """
    print("\n" + "="*60)
    print("SCHRITT 2: WebSocket-Versuche")
    print("="*60)

    pw_varianten = {
        "plain":      passwort,
        "md5":        md5(passwort),
        "md5_upper":  md5(passwort).upper(),
        "sha256":     hashlib.sha256(passwort.encode()).hexdigest(),
        "md5_md5":    md5(md5(passwort)),
        "md5_email":  md5(f"{email}:{passwort}"),
    }

    ssl_noverify = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_noverify.check_hostname = False
    ssl_noverify.verify_mode = ssl.CERT_NONE

    versuche = []
    for url in URLS[:2]:  # Nur die 2 wichtigsten URLs
        for pw_name, pw_wert in pw_varianten.items():
            versuche.append({
                "url": url,
                "headers": {
                    "Authorization": basic_auth(email, pw_wert),
                    "x-ssl-client-dn": f"emailAddress={email}",
                },
                "name": f"URL={url.split('?')[0][-30:]} | PW={pw_name}",
            })

    for v in versuche:
        print(f"\nVersuch: {v['name']}")
        try:
            async with websockets.connect(
                v["url"],
                additional_headers=v["headers"],
                open_timeout=10,
                ping_interval=None,
            ) as ws:
                print("  >>> VERBINDUNG ERFOLGREICH! <<<")
                # Geraete lesen
                await ws.send(erstelle_http_anfrage("GET", "/devices/data"))
                try:
                    antwort = await asyncio.wait_for(ws.recv(), timeout=8)
                    text = antwort if isinstance(antwort, str) else antwort.decode()
                    if "\r\n\r\n" in text:
                        body = text.split("\r\n\r\n", 1)[1]
                        daten = json.loads(body)
                        print(f"  GERAETE: {json.dumps(daten, ensure_ascii=False)[:500]}")
                except Exception as e:
                    print(f"  Lese-Fehler: {e}")
                return True
        except Exception as e:
            fehler = str(e)
            if "401" in fehler:
                print(f"  -> 401 (Zugangsdaten)")
            elif "400" in fehler:
                print(f"  -> 400 (Format)")
            elif "403" in fehler:
                print(f"  -> 403 (Verboten)")
            elif "404" in fehler:
                print(f"  -> 404 (URL nicht gefunden)")
            elif "timeout" in fehler.lower() or "TimeoutError" in type(e).__name__:
                print(f"  -> Timeout")
            else:
                print(f"  -> {type(e).__name__}: {fehler[:100]}")
        await asyncio.sleep(0.5)

    return False


async def schritt3_weitere_urls(email, passwort):
    """
    Testet weitere mögliche API-Endpunkte.
    """
    print("\n" + "="*60)
    print("SCHRITT 3: Weitere URL-Varianten")
    print("="*60)

    pw_md5 = md5(passwort)
    headers = {
        "Authorization": basic_auth(email, pw_md5),
        "x-ssl-client-dn": f"emailAddress={email}",
    }

    weitere_urls = [
        f"wss://medi.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1",
        f"wss://tydom.deltadore.fr/mediation/client?mac={TYDOM_MAC}&appli=1",
        f"wss://mediation.tydom.com/mediation/client?mac={TYDOM_MAC.lower()}&appli=1",
        f"wss://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=3",
    ]

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    for url in weitere_urls:
        print(f"\n{url}")
        try:
            async with websockets.connect(
                url,
                additional_headers=headers,
                open_timeout=10,
                ping_interval=None,
                ssl=ssl_ctx,
            ) as ws:
                print("  >>> VERBUNDEN! <<<")
                return url
        except Exception as e:
            fehler = str(e)
            if "401" in fehler:
                print(f"  -> 401 (Server erreichbar, aber Auth falsch)")
            elif "400" in fehler:
                print(f"  -> 400")
            elif "404" in fehler:
                print(f"  -> 404 (URL existiert nicht)")
            elif any(x in fehler for x in ["getaddrinfo", "Name or service", "nodename"]):
                print(f"  -> DNS-Fehler (Domain existiert nicht)")
            elif "timeout" in fehler.lower():
                print(f"  -> Timeout")
            else:
                print(f"  -> {type(e).__name__}: {fehler[:80]}")
    return None


async def main():
    email = os.environ.get("TYDOM_EMAIL", "")
    passwort = os.environ.get("TYDOM_PASSWORD", "")

    if not email or not passwort:
        print("FEHLER: TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        return

    print(f"TYDOM Discovery v4")
    print(f"MAC:   {TYDOM_MAC}")
    print(f"Email: {email[:4]}***")

    # HTTPS-Probe
    schritt1_https_probe(email, passwort)

    # WebSocket-Versuche
    ok = await schritt2_websocket_versuche(email, passwort)

    if not ok:
        await schritt3_weitere_urls(email, passwort)

    if not ok:
        print("\n" + "="*60)
        print("ZUSAMMENFASSUNG: Alle Versuche fehlgeschlagen")
        print("Naechster Schritt: Netzwerk-Mitschnitt der TYDOM-App")
        print("um die exakten Authentifizierungsdaten zu ermitteln.")
        print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
