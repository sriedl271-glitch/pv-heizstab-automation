"""
TYDOM Discovery Script - Version 7
Vollstaendiger 3-stufiger Authentifizierungsprozess:

STUFE 1: OAuth2-Login bei Delta Dore Azure B2C -> access_token
STUFE 2: Gateway-Passwort von Delta Dore API holen (mit access_token)
STUFE 3: WebSocket-Verbindung mit Digest Auth (MAC + Gateway-Passwort)

Erkenntnisquelle: hass-deltadore-tydom-component (GitHub, CyrilP)
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


TYDOM_MAC = "001A25067773"
MEDIATION_URL = "mediation.tydom.com"
TYDOM_WSS  = f"wss://{MEDIATION_URL}/mediation/client?mac={TYDOM_MAC}&appli=1"
TYDOM_HTTPS = f"https://{MEDIATION_URL}/mediation/client?mac={TYDOM_MAC}&appli=1"

# Delta Dore OAuth2 / B2C Konfiguration
DELTADORE_AUTH_URL  = (
    "https://deltadoreadb2ciot.b2clogin.com"
    "/deltadoreadb2ciot.onmicrosoft.com"
    "/v2.0/.well-known/openid-configuration"
    "?p=B2C_1_AccountProviderROPC_SignIn"
)
DELTADORE_CLIENT_ID = "8782839f-3264-472a-ab87-4d4e23524da4"
DELTADORE_SCOPE = (
    "openid profile offline_access "
    "https://deltadoreadb2ciot.onmicrosoft.com/iotapi/video_config "
    "https://deltadoreadb2ciot.onmicrosoft.com/iotapi/sites_management_allowed "
    "https://deltadoreadb2ciot.onmicrosoft.com/iotapi/sites_management_gateway_credentials "
    "https://deltadoreadb2ciot.onmicrosoft.com/iotapi/pilotage_allowed "
    "https://deltadoreadb2ciot.onmicrosoft.com/iotapi/tydom_backend_allowed "
    "https://deltadoreadb2ciot.onmicrosoft.com/iotapi/websocket_remote_access"
)
DELTADORE_API_SITES = (
    "https://prod.iotdeltadore.com/sitesmanagement/api/v1/sites?gateway_mac="
)


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def parse_www_authenticate(header: str) -> dict:
    params = {}
    for match in re.finditer(r'(\w+)="([^"]*)"', header):
        params[match.group(1)] = match.group(2)
    for match in re.finditer(r'(\w+)=([^",\s]+)', header):
        if match.group(1) not in params:
            params[match.group(1)] = match.group(2)
    return params


def berechne_digest(username, password, challenge_params, uri, methode="GET"):
    realm  = challenge_params.get("realm", "")
    nonce  = challenge_params.get("nonce", "")
    qop    = challenge_params.get("qop", "")
    opaque = challenge_params.get("opaque", "")

    nc     = "00000001"
    cnonce = secrets.token_hex(8)

    ha1 = md5(f"{username}:{realm}:{password}")
    ha2 = md5(f"{methode}:{uri}")

    if qop and "auth" in qop:
        response = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        header = (
            f'Digest username="{username}", realm="{realm}", '
            f'nonce="{nonce}", uri="{uri}", '
            f'qop={qop}, nc={nc}, cnonce="{cnonce}", response="{response}"'
        )
    else:
        response = md5(f"{ha1}:{nonce}:{ha2}")
        header = (
            f'Digest username="{username}", realm="{realm}", '
            f'nonce="{nonce}", uri="{uri}", response="{response}"'
        )

    if opaque:
        header += f', opaque="{opaque}"'

    return header


# ─────────────────────────────────────────────
# STUFE 1: OAuth2-Token holen
# ─────────────────────────────────────────────
def stufe1_oauth2_token(email, passwort):
    print("\n" + "=" * 60)
    print("STUFE 1: OAuth2-Login bei Delta Dore")
    print("=" * 60)

    # 1a: OpenID-Konfiguration abrufen -> token_endpoint
    print("1a) Hole token_endpoint von B2C...")
    try:
        req = urllib.request.Request(
            DELTADORE_AUTH_URL,
            headers={"User-Agent": "TydomApp/4.17.41"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            openid_config = json.loads(r.read().decode())
        token_endpoint = openid_config.get("token_endpoint", "")
        print(f"    token_endpoint = {token_endpoint[:70]}...")
    except Exception as e:
        print(f"    FEHLER: {type(e).__name__}: {e}")
        return None

    # 1b: Token anfordern
    print("1b) Fordere access_token an...")
    try:
        post_daten = urllib.parse.urlencode({
            "username":   email,
            "password":   passwort,
            "grant_type": "password",
            "client_id":  DELTADORE_CLIENT_ID,
            "scope":      DELTADORE_SCOPE,
        }).encode("utf-8")

        req = urllib.request.Request(
            token_endpoint,
            data=post_daten,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "TydomApp/4.17.41",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            token_antwort = json.loads(r.read().decode())

        access_token = token_antwort.get("access_token", "")
        if access_token:
            print(f"    access_token = {access_token[:30]}... (OK)")
            return access_token
        else:
            print(f"    Kein access_token in Antwort!")
            print(f"    Antwort: {json.dumps(token_antwort)[:300]}")
            return None

    except urllib.error.HTTPError as e:
        fehler_body = e.read().decode()
        print(f"    HTTP {e.code}: {fehler_body[:300]}")
        return None
    except Exception as e:
        print(f"    FEHLER: {type(e).__name__}: {e}")
        return None


# ─────────────────────────────────────────────
# STUFE 2: Gateway-Passwort holen
# ─────────────────────────────────────────────
def stufe2_gateway_passwort(access_token):
    print("\n" + "=" * 60)
    print("STUFE 2: Gateway-Passwort von Delta Dore API holen")
    print("=" * 60)

    url = DELTADORE_API_SITES + TYDOM_MAC
    print(f"URL: {url}")

    try:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "TydomApp/4.17.41",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            antwort = json.loads(r.read().decode())

        print(f"API-Antwort (vollstaendig):")
        print(json.dumps(antwort, indent=2, ensure_ascii=False)[:1000])

        # Gateway-Passwort aus Antwort extrahieren
        gw_passwort = None

        # Verschiedene moegliche Feldnamen probieren
        if isinstance(antwort, list) and len(antwort) > 0:
            site = antwort[0]
        elif isinstance(antwort, dict):
            site = antwort
        else:
            site = {}

        # Suche nach Passwort-Feldern
        for feld in ["gateway_password", "password", "pin", "gatewayPassword",
                     "gateway_pin", "credentials", "secret"]:
            if feld in site:
                gw_passwort = site[feld]
                print(f"\nGateway-Passwort gefunden: Feld '{feld}' = {str(gw_passwort)[:4]}***")
                break

        # In verschachtelten Strukturen suchen
        if not gw_passwort and "gateways" in site:
            for gw in site.get("gateways", []):
                for feld in ["password", "pin", "gateway_password", "credentials"]:
                    if feld in gw:
                        gw_passwort = gw[feld]
                        print(f"\nGateway-Passwort gefunden (in gateways): '{feld}' = {str(gw_passwort)[:4]}***")
                        break
                if gw_passwort:
                    break

        if not gw_passwort:
            print("\nKein Passwort-Feld gefunden!")
            print("Bitte pruefe die vollstaendige Ausgabe oben.")

        return gw_passwort

    except urllib.error.HTTPError as e:
        fehler_body = e.read().decode()
        print(f"HTTP {e.code}: {fehler_body[:300]}")
        return None
    except Exception as e:
        print(f"FEHLER: {type(e).__name__}: {e}")
        return None


# ─────────────────────────────────────────────
# STUFE 3: Digest-Challenge + WebSocket
# ─────────────────────────────────────────────
def stufe3a_challenge_holen():
    print("\n" + "=" * 60)
    print("STUFE 3a: Digest-Challenge holen")
    print("=" * 60)

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        TYDOM_HTTPS,
        headers={"User-Agent": "TydomApp/4.17.41"},
    )
    try:
        urllib.request.urlopen(req, context=ssl_ctx, timeout=10)
        print("Unerwartete 200-Antwort")
        return None
    except urllib.error.HTTPError as e:
        www_auth = e.headers.get("WWW-Authenticate", "")
        print(f"HTTP {e.code}")
        print(f"WWW-Authenticate: {www_auth}")
        if e.code == 401 and "Digest" in www_auth:
            params = parse_www_authenticate(www_auth)
            print(f"realm = {params.get('realm')}")
            print(f"qop   = {params.get('qop')}")
            print(f"nonce = {params.get('nonce', '')[:20]}...")
            return params
        return None
    except Exception as e:
        print(f"FEHLER: {type(e).__name__}: {e}")
        return None


async def stufe3b_websocket(gw_passwort, challenge_params):
    print("\n" + "=" * 60)
    print("STUFE 3b: WebSocket-Verbindung mit Gateway-Passwort")
    print("=" * 60)

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    uri_pfad = f"/mediation/client?mac={TYDOM_MAC}&appli=1"
    auth = berechne_digest(TYDOM_MAC, gw_passwort, challenge_params, uri_pfad)

    print(f"username = {TYDOM_MAC}")
    print(f"password = {str(gw_passwort)[:4]}***")

    try:
        async with websockets.connect(
            TYDOM_WSS,
            additional_headers={
                "Authorization": auth,
                "User-Agent": "TydomApp/4.17.41",
            },
            ssl=ssl_ctx,
            open_timeout=15,
            ping_interval=None,
        ) as ws:
            print("\n>>> VERBINDUNG ERFOLGREICH! <<<")

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
                        print(f"Antwort: {body[:300]}")
                else:
                    print(f"Antwort: {text[:300]}")
            except asyncio.TimeoutError:
                print("Timeout beim Lesen – Verbindung steht!")
            return True

    except Exception as e:
        fehler = str(e)
        print(f"-> {type(e).__name__}: {fehler[:200]}")
        return False


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    email    = os.environ.get("TYDOM_EMAIL", "")
    passwort = os.environ.get("TYDOM_PASSWORD", "")

    if not email or not passwort:
        print("FEHLER: TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        return

    print("TYDOM Discovery v7 – OAuth2 + Gateway-Passwort + Digest Auth")
    print(f"MAC:   {TYDOM_MAC}")
    print(f"Email: {email[:4]}***")

    # Stufe 1: OAuth2-Token
    access_token = stufe1_oauth2_token(email, passwort)
    if not access_token:
        print("\nOAuth2-Login fehlgeschlagen – Abbruch.")
        return

    # Stufe 2: Gateway-Passwort
    gw_passwort = stufe2_gateway_passwort(access_token)
    if not gw_passwort:
        print("\nGateway-Passwort nicht gefunden – Abbruch.")
        return

    # Stufe 3: Digest-Challenge + WebSocket
    challenge = stufe3a_challenge_holen()
    if not challenge:
        print("\nKeine Digest-Challenge erhalten – Abbruch.")
        return

    ok = await stufe3b_websocket(gw_passwort, challenge)

    print("\n" + "=" * 60)
    if ok:
        print("ERGEBNIS: VERBINDUNG ERFOLGREICH!")
        print("TYDOM-Steuerung kann jetzt in main.py eingebaut werden.")
    else:
        print("ERGEBNIS: Verbindung fehlgeschlagen.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
