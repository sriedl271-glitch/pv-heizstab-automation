"""
TYDOM Discovery Script - Version 3
Testet verschiedene Authentifizierungs-Varianten fuer die TYDOM API.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import ssl

import websockets

TYDOM_MAC = "001A25067773"
TYDOM_MAC_LOWER = "001a25067773"
TYDOM_URL_UPPER = f"wss://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1"
TYDOM_URL_LOWER = f"wss://mediation.tydom.com/mediation/client?mac={TYDOM_MAC_LOWER}&appli=1"


def mache_basic_auth(benutzername: str, passwort: str) -> str:
    credentials = base64.b64encode(f"{benutzername}:{passwort}".encode("utf-8")).decode("utf-8")
    return f"Basic {credentials}"


def erstelle_http_anfrage(methode: str, pfad: str, body: str = "") -> str:
    laenge = len(body.encode("utf-8")) if body else 0
    return (
        f"{methode} {pfad} HTTP/1.1\r\n"
        f"Content-Length: {laenge}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n"
        f"Transac-Id: 0\r\n"
        f"\r\n"
        f"{body}"
    )


async def verbinde(url: str, headers: dict, beschreibung: str) -> bool:
    print(f"\n{'='*55}")
    print(f"Versuch: {beschreibung}")
    print(f"URL:     {url}")
    print(f"{'='*55}")

    try:
        async with websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=15,
            ping_interval=None,
        ) as ws:
            print(">>> VERBINDUNG ERFOLGREICH! <<<")

            # Initiale Nachrichten lesen
            try:
                for _ in range(3):
                    msg = await asyncio.wait_for(ws.recv(), timeout=4)
                    text = msg if isinstance(msg, str) else msg.decode("utf-8")
                    print(f"Init-Msg ({len(text)} Bytes): {text[:200]}")
            except asyncio.TimeoutError:
                pass

            # Geraete-Konfiguration abfragen
            print("\n--- GET /configs/file ---")
            await ws.send(erstelle_http_anfrage("GET", "/configs/file"))
            antwort = ""
            try:
                for _ in range(8):
                    chunk = await asyncio.wait_for(ws.recv(), timeout=8)
                    antwort += chunk if isinstance(chunk, str) else chunk.decode("utf-8")
                    if len(antwort) > 100:
                        break
            except asyncio.TimeoutError:
                pass

            if "\r\n\r\n" in antwort:
                _, body = antwort.split("\r\n\r\n", 1)
                if body.strip():
                    try:
                        daten = json.loads(body)
                        print("CONFIGS/FILE:")
                        print(json.dumps(daten, indent=2, ensure_ascii=False)[:4000])
                    except Exception:
                        print(f"Body (roh): {body[:500]}")
            else:
                print(f"Antwort: {antwort[:400]}")

            # Geraete-Daten
            print("\n--- GET /devices/data ---")
            await ws.send(erstelle_http_anfrage("GET", "/devices/data"))
            antwort2 = ""
            try:
                for _ in range(8):
                    chunk = await asyncio.wait_for(ws.recv(), timeout=8)
                    antwort2 += chunk if isinstance(chunk, str) else chunk.decode("utf-8")
                    if len(antwort2) > 100:
                        break
            except asyncio.TimeoutError:
                pass

            if "\r\n\r\n" in antwort2:
                _, body2 = antwort2.split("\r\n\r\n", 1)
                if body2.strip():
                    try:
                        daten2 = json.loads(body2)
                        print("DEVICES/DATA:")
                        print(json.dumps(daten2, indent=2, ensure_ascii=False)[:4000])
                        if isinstance(daten2, list):
                            print("\n--- GERAETE-IDs ---")
                            for g in daten2:
                                print(f"Name: '{g.get('name')}' | ID: {g.get('id')}")
                                for ep in g.get("endpoints", []):
                                    print(f"  EP-ID: {ep.get('id')} | Name: {ep.get('name')} | Typen: {[s.get('name') for s in ep.get('cstatus', [])]}")
                    except Exception:
                        print(f"Body (roh): {body2[:500]}")

            # Szenarien
            print("\n--- GET /scenarios/file ---")
            await ws.send(erstelle_http_anfrage("GET", "/scenarios/file"))
            antwort3 = ""
            try:
                for _ in range(5):
                    chunk = await asyncio.wait_for(ws.recv(), timeout=8)
                    antwort3 += chunk if isinstance(chunk, str) else chunk.decode("utf-8")
                    if len(antwort3) > 100:
                        break
            except asyncio.TimeoutError:
                pass
            if "\r\n\r\n" in antwort3:
                _, body3 = antwort3.split("\r\n\r\n", 1)
                if body3.strip():
                    try:
                        daten3 = json.loads(body3)
                        print("SZENARIEN:")
                        print(json.dumps(daten3, indent=2, ensure_ascii=False)[:3000])
                    except Exception:
                        print(f"Body (roh): {body3[:400]}")

            return True

    except Exception as e:
        fehler = str(e)
        if "401" in fehler:
            print(f"FEHLER: HTTP 401 (Zugangsdaten falsch)")
        elif "400" in fehler:
            print(f"FEHLER: HTTP 400 (Anfrage-Format)")
        elif "403" in fehler:
            print(f"FEHLER: HTTP 403 (Zugriff verweigert)")
        elif "TimeoutError" in type(e).__name__ or "timeout" in fehler.lower():
            print(f"FEHLER: Timeout (kein Heimnetz-Zugriff von GitHub)")
        else:
            print(f"FEHLER: {type(e).__name__}: {fehler}")
        return False


async def main():
    email = os.environ.get("TYDOM_EMAIL", "")
    passwort = os.environ.get("TYDOM_PASSWORD", "")

    if not email or not passwort:
        print("FEHLER: TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        return

    print(f"TYDOM Discovery v3")
    print(f"MAC:   {TYDOM_MAC}")
    print(f"Email: {email[:4]}***")

    # Passwort-Varianten berechnen
    pw_plain     = passwort
    pw_md5       = hashlib.md5(passwort.encode("utf-8")).hexdigest()
    pw_md5_upper = pw_md5.upper()
    pw_sha256    = hashlib.sha256(passwort.encode("utf-8")).hexdigest()
    pw_sha1      = hashlib.sha1(passwort.encode("utf-8")).hexdigest()

    print(f"\nPasswort-Varianten:")
    print(f"  plain:     {pw_plain[:3]}***")
    print(f"  md5:       {pw_md5[:8]}...")
    print(f"  md5_upper: {pw_md5_upper[:8]}...")
    print(f"  sha256:    {pw_sha256[:8]}...")
    print(f"  sha1:      {pw_sha1[:8]}...")

    ssl_default = True
    ssl_noverify = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_noverify.check_hostname = False
    ssl_noverify.verify_mode = ssl.CERT_NONE

    versuche = [
        # (url, headers, beschreibung)
        (
            TYDOM_URL_UPPER,
            {"Authorization": mache_basic_auth(email, pw_md5),
             "x-ssl-client-dn": f"emailAddress={email}"},
            "1: MD5-Passwort + x-ssl-client-dn (MAC gross)"
        ),
        (
            TYDOM_URL_UPPER,
            {"Authorization": mache_basic_auth(email, pw_plain),
             "x-ssl-client-dn": f"emailAddress={email}"},
            "2: Klartext-Passwort + x-ssl-client-dn (MAC gross)"
        ),
        (
            TYDOM_URL_UPPER,
            {"Authorization": mache_basic_auth(email, pw_sha256),
             "x-ssl-client-dn": f"emailAddress={email}"},
            "3: SHA256-Passwort + x-ssl-client-dn (MAC gross)"
        ),
        (
            TYDOM_URL_UPPER,
            {"Authorization": mache_basic_auth(email, pw_sha1),
             "x-ssl-client-dn": f"emailAddress={email}"},
            "4: SHA1-Passwort + x-ssl-client-dn (MAC gross)"
        ),
        (
            TYDOM_URL_LOWER,
            {"Authorization": mache_basic_auth(email, pw_md5),
             "x-ssl-client-dn": f"emailAddress={email}"},
            "5: MD5-Passwort + x-ssl-client-dn (MAC klein)"
        ),
        (
            TYDOM_URL_UPPER,
            {"Authorization": mache_basic_auth(email, pw_md5_upper)},
            "6: MD5-Passwort GROSSBUCHSTABEN, kein x-ssl-header"
        ),
        (
            TYDOM_URL_UPPER,
            {"Authorization": mache_basic_auth(email, pw_plain)},
            "7: Klartext-Passwort, kein x-ssl-header"
        ),
    ]

    for url, headers, beschreibung in versuche:
        ok = await verbinde(url, headers, beschreibung)
        if ok:
            print(f"\n>>> ERFOLG mit Versuch: {beschreibung} <<<")
            return
        await asyncio.sleep(1)

    print("\n=== Alle Versuche fehlgeschlagen ===")
    print("Moegliche Ursachen:")
    print("1. Delta Dore verwendet einen eigenen Authentifizierungsserver (OAuth2)")
    print("2. Das TYDOM Home hat ein separates API-Passwort")
    print("3. Die Mediation-URL ist fuer dieses Geraet anders")


if __name__ == "__main__":
    asyncio.run(main())
