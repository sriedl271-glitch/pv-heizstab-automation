"""
TYDOM Discovery Script - Version 2
Verbindet sich mit dem TYDOM Home ueber die Delta Dore Cloud
und gibt alle Geraete, Endpunkte und Szenarien aus.
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


def erstelle_auth_header(email: str, passwort: str) -> str:
    passwort_md5 = hashlib.md5(passwort.encode("utf-8")).hexdigest()
    credentials = f"{email}:{passwort_md5}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return f"Basic {encoded}"


def erstelle_http_anfrage(methode: str, pfad: str, body: str = "") -> str:
    laenge = len(body.encode("utf-8")) if body else 0
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


def parse_antwort(rohdaten: str) -> object:
    try:
        if "\r\n\r\n" in rohdaten:
            _, body = rohdaten.split("\r\n\r\n", 1)
        else:
            body = rohdaten
        body = body.strip()
        if body:
            return json.loads(body)
        return None
    except Exception as fehler:
        print(f"  Parse-Fehler: {fehler}")
        print(f"  Rohdaten: {rohdaten[:300]}")
        return None


async def lese_nachrichten(ws, anzahl=5, timeout=8):
    """Liest bis zu 'anzahl' Nachrichten mit Timeout."""
    nachrichten = []
    try:
        for _ in range(anzahl):
            msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8")
            nachrichten.append(msg)
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        print(f"  Lesefehler: {e}")
    return nachrichten


async def sende_anfrage(ws, methode: str, pfad: str, body: str = "") -> str:
    """Sendet eine Anfrage und liest die Antwort."""
    anfrage = erstelle_http_anfrage(methode, pfad, body)
    await ws.send(anfrage)
    antwort_teile = await lese_nachrichten(ws, anzahl=8, timeout=10)
    return "".join(antwort_teile)


async def verbinde_tydom(url: str, headers: dict, ssl_kontext=None) -> bool:
    print(f"\n{'='*60}")
    print(f"Verbinde: {url}")
    print(f"{'='*60}")

    connect_kwargs = {
        "additional_headers": headers,
        "open_timeout": 20,
        "ping_interval": None,
        "ping_timeout": None,
    }
    if ssl_kontext:
        connect_kwargs["ssl"] = ssl_kontext

    try:
        async with websockets.connect(url, **connect_kwargs) as ws:
            print("OK - WebSocket verbunden")

            # Initiale Nachrichten lesen
            init_msgs = await lese_nachrichten(ws, anzahl=3, timeout=4)
            if init_msgs:
                print(f"  {len(init_msgs)} initiale Nachrichten empfangen")

            # 1. Geraete-Konfiguration
            print("\n--- Anfrage: /configs/file ---")
            antwort = await sende_anfrage(ws, "GET", "/configs/file")
            daten = parse_antwort(antwort)
            if daten:
                print("CONFIGS/FILE DATEN:")
                print(json.dumps(daten, indent=2, ensure_ascii=False)[:3000])
            else:
                print(f"Keine Daten. Rohantwort: {antwort[:400]}")

            # 2. Geraete-Daten
            print("\n--- Anfrage: /devices/data ---")
            antwort2 = await sende_anfrage(ws, "GET", "/devices/data")
            daten2 = parse_antwort(antwort2)
            if daten2:
                print("DEVICES/DATA DATEN:")
                print(json.dumps(daten2, indent=2, ensure_ascii=False)[:3000])

                # Zusammenfassung
                if isinstance(daten2, list):
                    print("\n--- GERAETE ZUSAMMENFASSUNG ---")
                    for g in daten2:
                        print(f"\nGeraet: '{g.get('name','?')}' | ID: {g.get('id','?')}")
                        for ep in g.get("endpoints", []):
                            typen = [s.get("name") for s in ep.get("cstatus", [])]
                            print(f"  Endpoint ID: {ep.get('id','?')} | Name: {ep.get('name','?')} | Typen: {typen}")
            else:
                print(f"Keine Daten. Rohantwort: {antwort2[:400]}")

            # 3. Szenarien
            print("\n--- Anfrage: /scenarios/file ---")
            antwort3 = await sende_anfrage(ws, "GET", "/scenarios/file")
            daten3 = parse_antwort(antwort3)
            if daten3:
                print("SCENARIOS DATEN:")
                print(json.dumps(daten3, indent=2, ensure_ascii=False)[:3000])
            else:
                print(f"Keine Daten. Rohantwort: {antwort3[:400]}")

            # 4. Momentane Geraete-Status
            print("\n--- Anfrage: /devices/cdata ---")
            antwort4 = await sende_anfrage(ws, "GET", "/devices/cdata")
            daten4 = parse_antwort(antwort4)
            if daten4:
                print("DEVICES/CDATA:")
                print(json.dumps(daten4, indent=2, ensure_ascii=False)[:3000])
            else:
                print(f"Rohantwort: {antwort4[:400]}")

            return True

    except Exception as e:
        fehler_typ = type(e).__name__
        print(f"FEHLER: {fehler_typ}: {e}")

        # HTTP-Statuscode aus Fehler extrahieren
        fehler_str = str(e)
        if "401" in fehler_str:
            print("  -> HTTP 401: Zugangsdaten falsch (E-Mail oder Passwort)")
        elif "400" in fehler_str:
            print("  -> HTTP 400: Anfrage-Format fehlerhaft")
        elif "403" in fehler_str:
            print("  -> HTTP 403: Zugriff verweigert")
        elif "404" in fehler_str:
            print("  -> HTTP 404: Endpunkt nicht gefunden")
        return False


async def main():
    email = os.environ.get("TYDOM_EMAIL", "")
    passwort = os.environ.get("TYDOM_PASSWORD", "")

    if not email or not passwort:
        print("FEHLER: TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        return

    print(f"TYDOM Discovery v2")
    print(f"MAC:   {TYDOM_MAC}")
    print(f"Email: {email[:4]}***")
    print(f"URL:   {TYDOM_URL}")

    # Versuch 1: MD5-Passwort (Standard TYDOM)
    print("\n=== Versuch 1: Basic Auth mit MD5-Passwort ===")
    auth_md5 = erstelle_auth_header(email, passwort)
    headers_v1 = {
        "Authorization": auth_md5,
        "x-ssl-client-dn": f"emailAddress={email}",
    }
    ok = await verbinde_tydom(TYDOM_URL, headers_v1)

    if not ok:
        # Versuch 2: Klartext-Passwort (ohne MD5)
        print("\n=== Versuch 2: Basic Auth mit Klartext-Passwort ===")
        credentials_plain = base64.b64encode(f"{email}:{passwort}".encode()).decode()
        auth_plain = f"Basic {credentials_plain}"
        headers_v2 = {
            "Authorization": auth_plain,
            "x-ssl-client-dn": f"emailAddress={email}",
        }
        ok = await verbinde_tydom(TYDOM_URL, headers_v2)

    if not ok:
        # Versuch 3: Ohne x-ssl-client-dn Header
        print("\n=== Versuch 3: Nur Authorization Header ===")
        headers_v3 = {"Authorization": auth_md5}
        await verbinde_tydom(TYDOM_URL, headers_v3)


if __name__ == "__main__":
    asyncio.run(main())
