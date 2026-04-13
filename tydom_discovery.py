"""
TYDOM Discovery Script
Verbindet sich mit dem TYDOM Home über die Delta Dore Cloud
und gibt alle Geräte, Endpunkte und Szenarien aus.

Wird einmalig ausgeführt um die Geräte-IDs zu ermitteln.
"""
import asyncio
import base64
import hashlib
import json
import os
import ssl

import websockets


TYDOM_MAC = "001A25067773"
TYDOM_URL = f"wss://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1"
TYDOM_LOCAL_IP = "192.168.178.24"
TYDOM_LOCAL_URL = f"wss://{TYDOM_LOCAL_IP}/mediation/client?mac={TYDOM_MAC}&appli=1"


def erstelle_auth_header(email: str, passwort: str) -> str:
    """
    TYDOM-Authentifizierung: email:md5(passwort) als Base64
    """
    passwort_md5 = hashlib.md5(passwort.encode("utf-8")).hexdigest()
    credentials = f"{email}:{passwort_md5}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


def erstelle_http_anfrage(methode: str, pfad: str, body: str = "") -> str:
    """
    Erstellt eine HTTP-formatierte Nachricht für den TYDOM WebSocket-Tunnel.
    """
    body_bytes = body.encode("utf-8") if body else b""
    laenge = len(body_bytes)
    anfrage = (
        f"{methode} {pfad} HTTP/1.1\r\n"
        f"Content-Length: {laenge}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n"
        f"Transac-Id: 0\r\n"
        f"\r\n"
    )
    if body:
        anfrage += body
    return anfrage


def parse_antwort(rohdaten: str) -> dict | None:
    """
    Parst die HTTP-formatierte Antwort aus dem WebSocket-Tunnel.
    Gibt den JSON-Body zurück oder None bei Fehler.
    """
    try:
        # Header und Body trennen
        if "\r\n\r\n" in rohdaten:
            _, body = rohdaten.split("\r\n\r\n", 1)
        else:
            body = rohdaten

        if body.strip():
            return json.loads(body)
        return None
    except Exception as fehler:
        print(f"Parsing-Fehler: {fehler}")
        print(f"Rohdaten (erste 500 Zeichen): {rohdaten[:500]}")
        return None


async def verbinde_und_erkunde(url: str, auth: str, ssl_kontext=None) -> bool:
    """
    Verbindet sich mit TYDOM und gibt alle Geräte/Szenarien aus.
    Gibt True zurück wenn erfolgreich.
    """
    print(f"\n{'='*60}")
    print(f"Verbinde mit: {url}")
    print(f"{'='*60}")

    headers = {
        "Authorization": auth,
        "Connection": "Upgrade",
        "Upgrade": "websocket",
    }

    try:
        kwargs = {
            "additional_headers": headers,
            "open_timeout": 15,
            "ping_interval": None,
        }
        if ssl_kontext:
            kwargs["ssl"] = ssl_kontext

        async with websockets.connect(url, **kwargs) as ws:
            print("✓ WebSocket-Verbindung hergestellt")

            # Initiale Nachrichten lesen (TYDOM sendet beim Connect oft Daten)
            initiale_nachrichten = []
            try:
                for _ in range(3):
                    msg = await asyncio.wait_for(ws.recv(), timeout=3)
                    initiale_nachrichten.append(msg)
                    print(f"  Initiale Nachricht empfangen ({len(msg)} Bytes)")
            except asyncio.TimeoutError:
                pass

            # Geräte abfragen
            print("\n--- Geraete-Abfrage (/devices/data) ---")
            anfrage = erstelle_http_anfrage("GET", "/devices/data")
            await ws.send(anfrage)

            antwort_text = ""
            try:
                for _ in range(5):
                    chunk = await asyncio.wait_for(ws.recv(), timeout=10)
                    antwort_text += chunk if isinstance(chunk, str) else chunk.decode("utf-8")
                    if "}" in antwort_text:
                        break
            except asyncio.TimeoutError:
                pass

            daten = parse_antwort(antwort_text)
            if daten:
                print(f"\nGefundene Geraete: {len(daten) if isinstance(daten, list) else 'Siehe JSON'}")
                print("\nVOLLSTAENDIGE GERAETE-DATEN:")
                print(json.dumps(daten, indent=2, ensure_ascii=False))

                # Zusammenfassung der Geraete
                if isinstance(daten, list):
                    print("\n--- GERAETE-ZUSAMMENFASSUNG ---")
                    for geraet in daten:
                        geraet_id = geraet.get("id", "?")
                        name = geraet.get("name", "?")
                        endpoints = geraet.get("endpoints", [])
                        print(f"\nGeraet: '{name}' | ID: {geraet_id}")
                        for ep in endpoints:
                            ep_id = ep.get("id", "?")
                            ep_name = ep.get("name", "?")
                            typen = [u.get("name") for u in ep.get("cstatus", [])]
                            print(f"  Endpoint: '{ep_name}' | ID: {ep_id} | Typen: {typen}")
            else:
                print("Keine Daten empfangen oder Parsing fehlgeschlagen")
                print(f"Rohantwort: {antwort_text[:1000]}")

            # Szenarien abfragen
            print("\n--- Szenarien-Abfrage (/scenarios) ---")
            anfrage2 = erstelle_http_anfrage("GET", "/scenarios/file")
            await ws.send(anfrage2)

            antwort2 = ""
            try:
                for _ in range(5):
                    chunk = await asyncio.wait_for(ws.recv(), timeout=10)
                    antwort2 += chunk if isinstance(chunk, str) else chunk.decode("utf-8")
                    if "}" in antwort2:
                        break
            except asyncio.TimeoutError:
                pass

            szenarien_daten = parse_antwort(antwort2)
            if szenarien_daten:
                print("\nVOLLSTAENDIGE SZENARIEN-DATEN:")
                print(json.dumps(szenarien_daten, indent=2, ensure_ascii=False))
            else:
                print(f"Rohantwort Szenarien: {antwort2[:500]}")

            return True

    except websockets.exceptions.InvalidStatusCode as e:
        print(f"✗ Verbindungsfehler - HTTP Status: {e.status_code}")
        if e.status_code == 401:
            print("  → Authentifizierung fehlgeschlagen (falsches Passwort / E-Mail)")
        elif e.status_code == 403:
            print("  → Zugriff verweigert")
        return False
    except Exception as fehler:
        print(f"✗ Fehler: {type(fehler).__name__}: {fehler}")
        return False


async def main():
    email = os.environ.get("TYDOM_EMAIL")
    passwort = os.environ.get("TYDOM_PASSWORD")

    if not email or not passwort:
        print("✗ TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        return

    print(f"TYDOM Discovery - MAC: {TYDOM_MAC}")
    print(f"E-Mail: {email}")

    auth = erstelle_auth_header(email, passwort)

    # Zuerst Cloud-Verbindung versuchen
    cloud_erfolgreich = await verbinde_und_erkunde(TYDOM_URL, auth)

    if not cloud_erfolgreich:
        print("\n--- Cloud-Verbindung fehlgeschlagen, versuche lokale Verbindung ---")
        ssl_kontext = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_kontext.check_hostname = False
        ssl_kontext.verify_mode = ssl.CERT_NONE
        await verbinde_und_erkunde(TYDOM_LOCAL_URL, auth, ssl_kontext)


if __name__ == "__main__":
    asyncio.run(main())
