"""
PV-Heizstab-Automation – Hauptscript v2.2
TYDOM-Steuerung via PUT + Schaltlogik + Morgen-/Abend-Report mit Tagesdiagramm
"""
import asyncio
import hashlib
import io
import json
import os
import re
import secrets as py_secrets
import smtplib
import ssl
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import websockets

# ═══════════════════════════════════════════════════════════════════════════════
# KONSTANTEN
# ═══════════════════════════════════════════════════════════════════════════════
STATUS_DATEI            = "status.json"
AUTOMATION_PAUSE_DATEI  = "automation_pause.json"
ISOLARCLOUD_API_URL     = "https://gateway.isolarcloud.eu"
GITHUB_REPO_URL         = "https://github.com/sriedl271-glitch/pv-heizstab-automation"

TYDOM_MAC   = "001A25067773"
TYDOM_WSS   = f"wss://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1"
TYDOM_HTTPS = f"https://mediation.tydom.com/mediation/client?mac={TYDOM_MAC}&appli=1"
CMD_PREFIX  = b"\x02"

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

# TYDOM Geraete- und Szenario-IDs (ermittelt 14.04.2026)
GERAET_6KW  = 1727103801
GERAET_3KW  = 1735296979
SCN_EIN_6KW = 327496727
SCN_AUS_6KW = 1066578186
SCN_EIN_3KW = 707579629
SCN_AUS_3KW = 389344593

# iSolarCloud Echtzeit-Messpunkte
MP_SOC          = "13141"
MP_HAUS         = "13119"
MP_NETZ_IMPORT  = "13149"
MP_EINSPEISUNG  = "13121"
MP_BAT_LADEN    = "13126"
MP_BAT_ENTLADEN = "13150"

# iSolarCloud Tages-Energiemesspunkte (offizielle Tageswerte wie in Sungrow-App)
MP_PV_HEUTE        = "13112"  # Daily PV Yield (Wh)
MP_EINSP_HEUTE     = "13122"  # Feed-in Energy Today (Wh)
MP_NETZ_HEUTE      = "13147"  # Energy Purchased Today (Wh)
MP_BAT_LAD_HEUTE   = "13028"  # Battery Charging Energy Today (Wh)
MP_BAT_ENTL_HEUTE  = "13029"  # Battery Discharging Energy Today (Wh)
MP_HAUS_HEUTE      = "13199"  # Daily Load Consumption (Wh)

# Saison 6kW: erlaubt 01.Oktober bis 15.Mai
SAISON_6KW_START = (10, 1)
SAISON_6KW_ENDE  = (5, 15)

# Pending-Mindestwartezeit in Sekunden
PENDING_SEKUNDEN = 180

# Zeitzone: CEST = UTC+2 (April–Oktober)
CEST_OFFSET = 2

# Berichtszeiten (UTC): 07:00 CEST = 05:00 UTC, 21:00 CEST = 19:00 UTC
MORGENREPORT_UTC_STUNDE = 5
ABENDREPORT_UTC_STUNDE  = 19
REPORT_FENSTER_MIN      = 10


# ═══════════════════════════════════════════════════════════════════════════════
# STATUS.JSON
# ═══════════════════════════════════════════════════════════════════════════════
def lade_status() -> dict:
    if not os.path.exists(STATUS_DATEI):
        return {}
    try:
        with open(STATUS_DATEI, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Fehler beim Laden von status.json: {e}")
        return {}

def speichere_status(status: dict) -> None:
    try:
        with open(STATUS_DATEI, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        print("✅ status.json gespeichert.")
    except Exception as e:
        print(f"❌ Fehler beim Speichern von status.json: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# BENACHRICHTIGUNGEN
# ═══════════════════════════════════════════════════════════════════════════════
def sende_pushover(titel: str, nachricht: str, prioritaet: int = 0) -> None:
    user_key  = os.environ.get("PUSHOVER_USER_KEY")
    api_token = os.environ.get("PUSHOVER_API_TOKEN")
    if not user_key or not api_token:
        print("❌ Pushover-Zugangsdaten fehlen!")
        return
    try:
        r = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": api_token, "user": user_key,
                  "title": titel, "message": nachricht, "priority": prioritaet},
            timeout=10,
        )
        print(f"Pushover: {r.status_code}")
    except Exception as e:
        print(f"❌ Pushover Fehler: {e}")

def sende_email(betreff: str, inhalt: str) -> None:
    passwort = os.environ.get("GMAIL_APP_PASSWORD")
    adresse  = "sriedl271@gmail.com"
    if not passwort:
        print("❌ GMAIL_APP_PASSWORD fehlt.")
        return
    msg = MIMEText(inhalt, "plain", "utf-8")
    msg["Subject"] = betreff
    msg["From"]    = adresse
    msg["To"]      = adresse
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(adresse, passwort)
            server.sendmail(adresse, adresse, msg.as_string())
        print("✅ E-Mail gesendet.")
    except Exception as e:
        print(f"❌ E-Mail Fehler: {e}")

def sende_email_mit_anhang(betreff: str, inhalt: str, png_bytes: bytes = None) -> None:
    """Sendet E-Mail mit optionalem PNG-Anhang (Tagesdiagramm)."""
    passwort = os.environ.get("GMAIL_APP_PASSWORD")
    adresse  = "sriedl271@gmail.com"
    if not passwort:
        print("❌ GMAIL_APP_PASSWORD fehlt.")
        return
    msg = MIMEMultipart()
    msg["Subject"] = betreff
    msg["From"]    = adresse
    msg["To"]      = adresse
    msg.attach(MIMEText(inhalt, "plain", "utf-8"))
    if png_bytes:
        img = MIMEImage(png_bytes, name="tagesdiagramm.png")
        img.add_header("Content-Disposition", "attachment", filename="tagesdiagramm.png")
        msg.attach(img)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(adresse, passwort)
            server.sendmail(adresse, adresse, msg.as_string())
        print("✅ E-Mail mit Anhang gesendet.")
    except Exception as e:
        print(f"❌ E-Mail Fehler: {e}")

def benachrichtige(titel: str, text: str, prioritaet: int = 0) -> None:
    zeit   = (datetime.utcnow() + timedelta(hours=CEST_OFFSET)).strftime("%d.%m.%Y %H:%M")
    inhalt = f"Zeit: {zeit}\n\n{text}"
    sende_pushover(titel, inhalt, prioritaet)
    sende_email(titel, inhalt)


# ═══════════════════════════════════════════════════════════════════════════════
# iSOLARCLOUD API
# ═══════════════════════════════════════════════════════════════════════════════
def _isc_headers(secret_key: str) -> dict:
    return {"Content-Type": "application/json;charset=UTF-8",
            "x-access-key": secret_key, "sys_code": "901"}

def isolarcloud_login(app_key, secret_key, user_account, user_password):
    try:
        r = requests.post(
            f"{ISOLARCLOUD_API_URL}/openapi/login",
            json={"appkey": app_key, "user_account": user_account,
                  "user_password": user_password},
            headers=_isc_headers(secret_key), timeout=15)
        d = r.json()
        if d.get("result_code") == "1":
            print("✅ iSolarCloud Login erfolgreich.")
            return d["result_data"]["token"]
        print(f"❌ iSolarCloud Login: {d.get('result_msg')}")
    except Exception as e:
        print(f"❌ iSolarCloud Login Fehler: {e}")
    return None

def isolarcloud_get_ps_id(app_key, secret_key, token):
    try:
        r = requests.post(
            f"{ISOLARCLOUD_API_URL}/openapi/getPowerStationList",
            json={"appkey": app_key, "token": token, "curPage": 1, "size": 10},
            headers=_isc_headers(secret_key), timeout=15)
        d = r.json()
        if d.get("result_code") == "1":
            anlagen = d["result_data"]["pageList"]
            if anlagen:
                ps_id = str(anlagen[0]["ps_id"])
                print(f"✅ Anlage: ps_id={ps_id}, Name={anlagen[0].get('ps_name','?')}")
                return ps_id
    except Exception as e:
        print(f"❌ ps_id Fehler: {e}")
    return None

def isolarcloud_get_device_info(app_key, secret_key, token, ps_id):
    try:
        r = requests.post(
            f"{ISOLARCLOUD_API_URL}/openapi/getDeviceList",
            json={"appkey": app_key, "token": token, "ps_id": ps_id,
                  "curPage": 1, "size": 50, "is_virtual_unit": "0"},
            headers=_isc_headers(secret_key), timeout=15)
        d = r.json()
        if d.get("result_code") == "1":
            geraete = d["result_data"]["pageList"]
            print(f"✅ Geraete: {len(geraete)} gefunden")
            for g in geraete:
                print(f"   Typ {g.get('device_type'):>3}: {g.get('device_name')} | ps_key={g.get('ps_key')}")
            for pref_typ in [14, 22]:
                for g in geraete:
                    if g.get("device_type") == pref_typ:
                        return g.get("ps_key"), pref_typ
            if geraete:
                g = geraete[0]
                return g.get("ps_key"), g.get("device_type")
    except Exception as e:
        print(f"❌ Device-Info Fehler: {e}")
    return None, None

def hole_isolarcloud_daten(app_key, secret_key, user_account, user_password):
    token = isolarcloud_login(app_key, secret_key, user_account, user_password)
    if not token:
        return None
    ps_id = isolarcloud_get_ps_id(app_key, secret_key, token)
    if not ps_id:
        return None
    ps_key, device_type = isolarcloud_get_device_info(app_key, secret_key, token, ps_id)
    if not ps_key:
        return None
    try:
        r = requests.post(
            f"{ISOLARCLOUD_API_URL}/openapi/getDeviceRealTimeData",
            json={"appkey": app_key, "token": token,
                  "ps_key_list": [ps_key], "device_type": device_type,
                  "point_id_list": [MP_SOC, MP_HAUS, MP_NETZ_IMPORT,
                                    MP_EINSPEISUNG, MP_BAT_LADEN, MP_BAT_ENTLADEN,
                                    MP_PV_HEUTE, MP_EINSP_HEUTE, MP_NETZ_HEUTE,
                                    MP_BAT_LAD_HEUTE, MP_BAT_ENTL_HEUTE, MP_HAUS_HEUTE]},
            headers=_isc_headers(secret_key), timeout=15)
        d = r.json()
        if d.get("result_code") == "1":
            ep = d["result_data"]["device_point_list"][0]["device_point"]
            def w(key):
                return int(round(float(ep.get(f"p{key}") or 0)))
            def kwh(key):
                return round(float(ep.get(f"p{key}") or 0) / 1000, 1)
            soc          = int(round(float(ep.get(f"p{MP_SOC}") or 0) * 100))
            haus         = w(MP_HAUS)
            netz_import  = w(MP_NETZ_IMPORT)
            einspeisung  = w(MP_EINSPEISUNG)
            bat_laden    = w(MP_BAT_LADEN)
            bat_entladen = w(MP_BAT_ENTLADEN)
            pv           = max(0, haus + einspeisung + bat_laden - netz_import - bat_entladen)
            ueberschuss  = max(0, einspeisung + bat_laden - netz_import - bat_entladen)
            tages_energie = {
                "pv_kwh":          kwh(MP_PV_HEUTE),
                "einspeisung_kwh": kwh(MP_EINSP_HEUTE),
                "netzbezug_kwh":   kwh(MP_NETZ_HEUTE),
                "bat_laden_kwh":   kwh(MP_BAT_LAD_HEUTE),
                "bat_entladen_kwh":kwh(MP_BAT_ENTL_HEUTE),
                "haus_kwh":        kwh(MP_HAUS_HEUTE),
            }
            print(f"✅ iSolarCloud: SOC={soc}% PV={pv}W Netz={netz_import}W "
                  f"Einsp.={einspeisung}W Uebers.={ueberschuss}W")
            print(f"   Tagesdaten: PV={tages_energie['pv_kwh']}kWh "
                  f"Einsp.={tages_energie['einspeisung_kwh']}kWh "
                  f"Netz={tages_energie['netzbezug_kwh']}kWh "
                  f"Bat.Lad.={tages_energie['bat_laden_kwh']}kWh "
                  f"Bat.Entl.={tages_energie['bat_entladen_kwh']}kWh")
            return {"batterie_prozent": soc, "pv_leistung_w": pv,
                    "hausverbrauch_w": haus, "netzbezug_w": netz_import,
                    "einspeisung_w": einspeisung, "ueberschuss_w": ueberschuss,
                    "bat_laden_w": bat_laden, "bat_entladen_w": bat_entladen,
                    "tages_energie": tages_energie,
                    "_ps_key": ps_key, "_device_type": device_type, "_token": token,
                    "_app_key": app_key, "_secret_key": secret_key}
        print(f"❌ iSolarCloud Echtdaten: {d.get('result_msg')}")
    except Exception as e:
        print(f"❌ iSolarCloud Echtdaten Fehler: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# TYDOM AUTHENTIFIZIERUNG
# ═══════════════════════════════════════════════════════════════════════════════
def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()

def _transac_id() -> str:
    return str(time.time_ns() // 1_000_000)

def _parse_www_auth(header: str) -> dict:
    params = {}
    for m in re.finditer(r'(\w+)="([^"]*)"', header):
        params[m.group(1)] = m.group(2)
    for m in re.finditer(r'(\w+)=([^",\s]+)', header):
        if m.group(1) not in params:
            params[m.group(1)] = m.group(2)
    return params

def _berechne_digest(username: str, password: str, cp: dict, uri: str) -> str:
    realm  = cp.get("realm", "")
    nonce  = cp.get("nonce", "")
    qop    = cp.get("qop", "")
    nc     = "00000001"
    cnonce = py_secrets.token_hex(8)
    ha1    = _md5(f"{username}:{realm}:{password}")
    ha2    = _md5(f"GET:{uri}")
    if qop and "auth" in qop:
        resp = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        hdr  = (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
                f'uri="{uri}", qop={qop}, nc={nc}, cnonce="{cnonce}", response="{resp}"')
    else:
        resp = _md5(f"{ha1}:{nonce}:{ha2}")
        hdr  = (f'Digest username="{username}", realm="{realm}", nonce="{nonce}", '
                f'uri="{uri}", response="{resp}"')
    if cp.get("opaque"):
        hdr += f', opaque="{cp["opaque"]}"'
    return hdr

def tydom_oauth2_token(email: str, passwort: str) -> str:
    req = urllib.request.Request(DELTADORE_AUTH_URL,
                                 headers={"User-Agent": "TydomApp/4.17.41"})
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

def tydom_gateway_passwort(access_token: str) -> str:
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
                res = suche(v)
                if res:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = suche(item)
                if res:
                    return res
        return None
    return suche(data)

def tydom_digest_challenge() -> dict:
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode    = ssl.CERT_NONE
    req = urllib.request.Request(TYDOM_HTTPS,
                                 headers={"User-Agent": "TydomApp/4.17.41"})
    try:
        urllib.request.urlopen(req, context=ssl_ctx, timeout=10)
    except urllib.error.HTTPError as e:
        www_auth = e.headers.get("WWW-Authenticate", "")
        if e.code == 401 and "Digest" in www_auth:
            return _parse_www_auth(www_auth)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# TYDOM WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════
def _http_msg(methode: str, pfad: str, body: str = "") -> bytes:
    laenge = len(body.encode("utf-8")) if body else 0
    msg = (f"{methode} {pfad} HTTP/1.1\r\n"
           f"Content-Length: {laenge}\r\n"
           f"Content-Type: application/json; charset=UTF-8\r\n"
           f"Transac-Id: {_transac_id()}\r\n\r\n{body}")
    return CMD_PREFIX + msg.encode("ascii")

def _dekodiere_chunked(body: str) -> str:
    result = []
    data   = body.encode("utf-8", errors="replace")
    i = 0
    while i < len(data):
        crlf = data.find(b"\r\n", i)
        if crlf == -1:
            break
        size_str = data[i:crlf].decode("ascii", errors="ignore").strip()
        if not size_str:
            i = crlf + 2
            continue
        try:
            chunk_size = int(size_str, 16)
        except ValueError:
            i = crlf + 2
            continue
        if chunk_size == 0:
            break
        i = crlf + 2
        result.append(data[i:i + chunk_size].decode("utf-8", errors="replace"))
        i += chunk_size + 2
    return "".join(result)

def _parse_nachricht(raw) -> tuple:
    data = raw if isinstance(raw, bytes) else raw.encode()
    if data and data[0] == 0x02:
        data = data[1:]
    text = data.decode("utf-8", errors="replace")
    if "\r\n\r\n" not in text:
        return None, None
    header, body = text.split("\r\n\r\n", 1)
    status_line  = header.split("\r\n")[0]
    try:
        return status_line, json.loads(body.strip())
    except (json.JSONDecodeError, ValueError):
        pass
    clean = _dekodiere_chunked(body)
    try:
        return status_line, json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        return status_line, None

def _extrahiere_geraetezustand(device_data: list) -> dict:
    zustand = {"3kw_ein": None, "6kw_ein": None}
    for geraet in device_data:
        gid = geraet.get("id")
        for ep in geraet.get("endpoints", []):
            for dp in ep.get("data", []):
                if dp.get("name") == "level" and dp.get("validity") == "upToDate":
                    ist_ein = float(dp.get("value", 0)) >= 50
                    if gid == GERAET_6KW:
                        zustand["6kw_ein"] = ist_ein
                    elif gid == GERAET_3KW:
                        zustand["3kw_ein"] = ist_ein
    return zustand

async def _tydom_async(gw_pw: str, challenge: dict, szenarien_ids: list = None) -> dict:
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode    = ssl.CERT_NONE
    uri_pfad = f"/mediation/client?mac={TYDOM_MAC}&appli=1"
    auth     = _berechne_digest(TYDOM_MAC, gw_pw, challenge, uri_pfad)

    async with websockets.connect(
        TYDOM_WSS,
        additional_headers={"Authorization": auth, "User-Agent": "TydomApp/4.17.41"},
        ssl=ssl_ctx, open_timeout=20, ping_interval=10,
    ) as ws:
        # Vollstaendige Init-Sequenz
        for methode, pfad in [
            ("GET",  "/ping"),
            ("GET",  "/info"),
            ("POST", "/refresh/all"),
            ("GET",  "/configs/file"),
            ("GET",  "/devices/data"),
            ("GET",  "/scenarios/file"),
        ]:
            await ws.send(_http_msg(methode, pfad))
            await asyncio.sleep(0.3)

        # Init-Antworten sammeln und Endpoint-IDs erfassen
        ep_map = {}
        device_data = None
        start = time.time()
        while time.time() - start < 8:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                _, body = _parse_nachricht(raw)
                if isinstance(body, list) and any("endpoints" in str(g) for g in body):
                    device_data = body
                    for geraet in body:
                        gid = geraet.get("id")
                        if gid in (GERAET_3KW, GERAET_6KW):
                            for ep in geraet.get("endpoints", []):
                                ep_id = ep.get("id")
                                for dp in ep.get("data", []):
                                    if dp.get("name") == "level":
                                        ep_map[gid] = ep_id
                                        break
            except asyncio.TimeoutError:
                if time.time() - start > 5:
                    break
        if ep_map:
            print(f"   Endpoint-IDs: 3kW={ep_map.get(GERAET_3KW)}, 6kW={ep_map.get(GERAET_6KW)}")
        else:
            print("   ⚠️  Keine Endpoint-IDs gefunden – Fallback auf Szenarien")

        # Geraete direkt schalten: PUT statt Szenario
        if szenarien_ids:
            SZENARIO_ZU_BEFEHL = {
                SCN_EIN_3KW: (GERAET_3KW, 100),
                SCN_AUS_3KW: (GERAET_3KW,   0),
                SCN_EIN_6KW: (GERAET_6KW, 100),
                SCN_AUS_6KW: (GERAET_6KW,   0),
            }
            for scn_id in szenarien_ids:
                befehl = SZENARIO_ZU_BEFEHL.get(scn_id)
                if not befehl:
                    continue
                device_id, level = befehl
                ep_id = ep_map.get(device_id)
                if ep_id is not None:
                    body_str = json.dumps([{"name": "level", "value": level}])
                    pfad = f"/devices/{device_id}/endpoints/{ep_id}/data"
                    await ws.send(_http_msg("PUT", pfad, body_str))
                    print(f"   ▶️  PUT {pfad} level={level}")
                else:
                    await ws.send(_http_msg("POST", f"/scenarios/{scn_id}/leftover", "{}"))
                    print(f"   ▶️  Szenario {scn_id} gesendet (Fallback)")
                await asyncio.sleep(0.5)
            await asyncio.sleep(10)
            await ws.send(_http_msg("GET", "/devices/data"))
            await asyncio.sleep(0.3)
            start2 = time.time()
            while time.time() - start2 < 15:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    _, body = _parse_nachricht(raw)
                    if isinstance(body, list) and any("endpoints" in str(g) for g in body):
                        device_data = body
                except asyncio.TimeoutError:
                    if time.time() - start2 > 8:
                        break

    if not device_data:
        print("❌ TYDOM: Keine Gerätedaten empfangen.")
        return None

    zustand = _extrahiere_geraetezustand(device_data)
    print(f"✅ TYDOM Zustand: 3kW={'EIN' if zustand['3kw_ein'] else 'AUS'}, "
          f"6kW={'EIN' if zustand['6kw_ein'] else 'AUS'}")
    return zustand

def tydom_ausfuehren(email: str, passwort: str, szenarien_ids: list = None) -> dict:
    """Vollstaendige TYDOM-Operation: Auth + Connect + Lesen/Schalten."""
    try:
        token     = tydom_oauth2_token(email, passwort)
        gw_pw     = tydom_gateway_passwort(token)
        challenge = tydom_digest_challenge()
        if not gw_pw or not challenge:
            print("❌ TYDOM Auth fehlgeschlagen.")
            return None
        print(f"✅ TYDOM Auth OK (realm={challenge.get('realm')})")
        return asyncio.run(_tydom_async(gw_pw, challenge, szenarien_ids))
    except Exception as e:
        print(f"❌ TYDOM Fehler: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# SOC-VERLAUF UND LADEGESCHWINDIGKEIT
# ═══════════════════════════════════════════════════════════════════════════════
def aktualisiere_soc_verlauf(status: dict, soc: int) -> list:
    verlauf = status.get("soc_verlauf", [])
    verlauf.append({"soc": soc, "zeit": datetime.utcnow().isoformat()})
    if len(verlauf) > 12:
        verlauf = verlauf[-12:]
    return verlauf

def berechne_laderate(soc_verlauf: list):
    """SOC-Anstieg in %/Stunde. None wenn < 3 Messpunkte."""
    if len(soc_verlauf) < 3:
        return None
    try:
        alt    = soc_verlauf[0]
        neu    = soc_verlauf[-1]
        t_alt  = datetime.fromisoformat(alt["zeit"])
        t_neu  = datetime.fromisoformat(neu["zeit"])
        diff_h = (t_neu - t_alt).total_seconds() / 3600
        if diff_h < 0.04:
            return None
        return (neu["soc"] - alt["soc"]) / diff_h
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# TAGES-VERLAUF (fuer Diagramm und Energieberechnung)
# ═══════════════════════════════════════════════════════════════════════════════
def lokal_jetzt() -> datetime:
    """Gibt aktuelle Lokalzeit (CEST = UTC+2) zurueck."""
    return datetime.utcnow() + timedelta(hours=CEST_OFFSET)

def reset_wenn_neuer_tag(status: dict) -> None:
    """Setzt Tagesdaten zurueck wenn ein neuer Tag begonnen hat."""
    heute_str = lokal_jetzt().strftime("%Y-%m-%d")
    if status.get("tages_datum") != heute_str:
        status["tages_datum"]        = heute_str
        status["tages_verlauf"]      = []
        status["schaltpunkte_heute"] = []
        print(f"🌅 Neuer Tag: {heute_str} – Tagesdaten zurückgesetzt")

def aktualisiere_tages_verlauf(status: dict, daten: dict) -> None:
    """Speichert aktuellen Messwert im Tages-Verlauf fuer Diagramm."""
    verlauf = status.get("tages_verlauf", [])
    verlauf.append({
        "zeit":          lokal_jetzt().strftime("%H:%M"),
        "soc":           daten["batterie_prozent"],
        "pv_w":          daten["pv_leistung_w"],
        "haus_w":        daten["hausverbrauch_w"],
        "einspeisung_w": daten["einspeisung_w"],
        "netzbezug_w":   daten["netzbezug_w"],
        "bat_laden_w":   daten.get("bat_laden_w", 0),
        "bat_entladen_w":daten.get("bat_entladen_w", 0),
        "ueberschuss_w": daten["ueberschuss_w"],
    })
    status["tages_verlauf"] = verlauf

def erfasse_schaltpunkt(status: dict, geraet: str, aktion: str,
                        soc: int, pv_w: int, modus: str = None) -> None:
    """Speichert Schaltpunkt fuer Diagramm-Annotation."""
    schaltpunkte = status.get("schaltpunkte_heute", [])
    schaltpunkte.append({
        "zeit":   lokal_jetzt().strftime("%H:%M"),
        "geraet": geraet,
        "aktion": aktion,
        "soc":    soc,
        "pv_w":   pv_w,
        "modus":  modus,
    })
    status["schaltpunkte_heute"] = schaltpunkte

def berechne_tages_energie(tages_verlauf: list) -> dict:
    """Berechnet Tages-Energie in kWh aus 5-Minuten-Messungen."""
    if not tages_verlauf:
        return {"pv_kwh": 0.0, "haus_kwh": 0.0, "einspeisung_kwh": 0.0,
                "netzbezug_kwh": 0.0, "bat_laden_kwh": 0.0, "bat_entladen_kwh": 0.0}
    dt_h = 5 / 60
    return {
        "pv_kwh":          round(sum(m.get("pv_w", 0)          for m in tages_verlauf) * dt_h / 1000, 1),
        "haus_kwh":        round(sum(m.get("haus_w", 0)         for m in tages_verlauf) * dt_h / 1000, 1),
        "einspeisung_kwh": round(sum(m.get("einspeisung_w", 0)  for m in tages_verlauf) * dt_h / 1000, 1),
        "netzbezug_kwh":   round(sum(m.get("netzbezug_w", 0)    for m in tages_verlauf) * dt_h / 1000, 1),
        "bat_laden_kwh":   round(sum(m.get("bat_laden_w", 0)    for m in tages_verlauf) * dt_h / 1000, 1),
        "bat_entladen_kwh":round(sum(m.get("bat_entladen_w", 0) for m in tages_verlauf) * dt_h / 1000, 1),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SAISON UND PAUSEN
# ═══════════════════════════════════════════════════════════════════════════════
def ist_6kw_saison() -> bool:
    m, d = lokal_jetzt().month, lokal_jetzt().day
    if m >= 10:
        return True
    if m < 5:
        return True
    if m == 5 and d <= 15:
        return True
    return False

def ist_automation_pausiert() -> bool:
    if not os.path.exists(AUTOMATION_PAUSE_DATEI):
        return False
    try:
        with open(AUTOMATION_PAUSE_DATEI, "r", encoding="utf-8") as f:
            pause = json.load(f)
        pause_bis = datetime.strptime(pause.get("pause_bis", ""), "%Y-%m-%d").date()
        if lokal_jetzt().date() <= pause_bis:
            print(f"⏸️  Automatik pausiert bis {pause_bis} (Grund: {pause.get('grund','?')})")
            return True
    except Exception:
        pass
    return False

def ist_manuell_pausiert(status: dict) -> bool:
    sperre = status.get("manuell_sperre_bis")
    if not sperre:
        return False
    try:
        bis = datetime.fromisoformat(sperre)
        if datetime.utcnow() < bis:
            bis_lokal = bis + timedelta(hours=CEST_OFFSET)
            print(f"⏸️  2h-Sperre aktiv bis {bis_lokal.strftime('%H:%M')} Uhr")
            return True
        status["manuell_sperre_bis"] = None
    except Exception:
        status["manuell_sperre_bis"] = None
    return False

def ist_pending_bestaetigt(pending_seit: str) -> bool:
    if not pending_seit:
        return False
    try:
        return (datetime.utcnow() - datetime.fromisoformat(pending_seit)).total_seconds() >= PENDING_SEKUNDEN
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# SCHALTBEDINGUNGEN
# ═══════════════════════════════════════════════════════════════════════════════
def pruefe_3kw_einschalten(daten: dict, status: dict, laderate) -> tuple:
    """Returns (soll_ein, modus, sofort)"""
    soc         = daten["batterie_prozent"]
    pv          = daten["pv_leistung_w"]
    ueberschuss = daten["ueberschuss_w"]
    einspeisung = daten.get("einspeisung_w", 0)

    if einspeisung > 0 and soc >= 99 and pv >= 1000:
        return True, "EINSPEISUNG_STOPP", True

    if status.get("soc_abschaltung_3kw"):
        if soc >= 85 and laderate is not None and laderate > 0:
            return True, "NORMAL", False
        return False, None, False

    if soc >= 97 and pv >= 2000:
        return True, "HOCHSPEICHER", False

    if laderate is None or laderate <= 0:
        return False, None, False

    if laderate >= 20 and soc >= 45 and ueberschuss >= 3200:
        return True, "NORMAL", False
    if laderate >= 15 and soc >= 65 and ueberschuss >= 3200:
        return True, "NORMAL", False
    if laderate > 0  and soc >= 75 and ueberschuss >= 3200:
        return True, "NORMAL", False

    return False, None, False

def pruefe_3kw_ausschalten(daten: dict, status: dict) -> bool:
    soc         = daten["batterie_prozent"]
    ueberschuss = daten["ueberschuss_w"]
    netzbezug   = daten["netzbezug_w"]
    pv          = daten["pv_leistung_w"]
    modus       = status.get("modus_3kw", "NORMAL")

    if netzbezug > 300:
        return True
    if ueberschuss < 1000:
        return True
    if soc < 70:
        return True
    if modus == "HOCHSPEICHER" and (soc < 85 or pv < 1000):
        return True
    if modus == "EINSPEISUNG_STOPP" and (soc < 95 or pv < 1000):
        status["modus_3kw"] = "NORMAL"
        return False
    return False

def pruefe_6kw_einschalten(daten: dict, status: dict, laderate) -> tuple:
    """Returns (soll_ein, modus, sofort)"""
    if not ist_6kw_saison():
        return False, None, False

    soc         = daten["batterie_prozent"]
    pv          = daten["pv_leistung_w"]
    ueberschuss = daten["ueberschuss_w"]

    if status.get("soc_abschaltung_6kw"):
        if soc >= 99 and laderate is not None and laderate >= 0:
            return True, "NORMAL", False
        return False, None, False

    if soc >= 99 and pv >= 4500:
        return True, "HOCHSPEICHER", False

    if laderate is None or laderate <= 0:
        return False, None, False

    if laderate >= 20 and soc >= 75 and ueberschuss >= 6300:
        return True, "NORMAL", False
    if laderate >= 15 and soc >= 83 and ueberschuss >= 6300:
        return True, "NORMAL", False
    if laderate > 0  and soc >= 90 and ueberschuss >= 6300:
        return True, "NORMAL", False

    return False, None, False

def pruefe_6kw_ausschalten(daten: dict, status: dict) -> bool:
    soc         = daten["batterie_prozent"]
    ueberschuss = daten["ueberschuss_w"]
    netzbezug   = daten["netzbezug_w"]
    pv          = daten["pv_leistung_w"]
    modus       = status.get("modus_6kw", "NORMAL")

    if netzbezug > 300:
        return True
    if ueberschuss < 4000:
        return True
    if soc < 80:
        return True
    if modus == "HOCHSPEICHER" and (soc < 93 or pv < 4500):
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# SCHALTLOGIK – HAUPTFUNKTION
# ═══════════════════════════════════════════════════════════════════════════════
def verarbeite_schaltlogik(daten: dict, status: dict, tydom_zustand: dict) -> tuple:
    """
    Wendet vollstaendige Schaltlogik an.
    Returns: (szenarien_ids: list, meldungen: list)
    """
    now_str     = datetime.utcnow().isoformat()
    szenarien   = []
    meldungen   = []
    soc         = daten["batterie_prozent"]
    ueberschuss = daten["ueberschuss_w"]
    netzbezug   = daten["netzbezug_w"]
    pv_w        = daten["pv_leistung_w"]

    # Tagesstatistik
    heute_str   = lokal_jetzt().strftime("%Y-%m-%d")
    schaltungen = status.get("schaltungen_heute", {})
    if schaltungen.get("datum") != heute_str:
        schaltungen = {"datum": heute_str,
                       "ein_3kw": 0, "aus_3kw": 0,
                       "ein_6kw": 0, "aus_6kw": 0}
    status["schaltungen_heute"] = schaltungen

    # ── Manuelle Eingriffe erkennen ──────────────────────────────────────────
    ist_3kw_ein = status.get("heizstab_3kw_ein", False)
    ist_6kw_ein = status.get("heizstab_6kw_ein", False)
    tydom_3kw   = tydom_zustand.get("3kw_ein", ist_3kw_ein)
    tydom_6kw   = tydom_zustand.get("6kw_ein", ist_6kw_ein)

    letzte_schalt = status.get("letzte_schaltzeit")
    schalt_kuerzlich = False
    if letzte_schalt:
        try:
            diff = (datetime.utcnow() - datetime.fromisoformat(letzte_schalt)).total_seconds()
            schalt_kuerzlich = diff < 900
        except Exception:
            pass

    if tydom_3kw != ist_3kw_ein:
        if not tydom_3kw and ist_3kw_ein:
            if schalt_kuerzlich:
                print("ℹ️  3kW AUS nach Schaltbefehl – kein 2h-Lock")
            else:
                bis_lokal = (datetime.utcnow() + timedelta(hours=2+CEST_OFFSET)).strftime("%H:%M")
                status["manuell_sperre_bis"] = (datetime.utcnow() + timedelta(hours=2)).isoformat()
                status["modus_3kw"] = None
                msg = f"🖐️ 3kW manuell ausgeschaltet – Automatik gesperrt bis {bis_lokal} Uhr"
                print(msg); meldungen.append(msg)
        else:
            print("ℹ️  3kW manuell EIN erkannt – Automatik laeuft weiter.")
        status["heizstab_3kw_ein"] = tydom_3kw

    if tydom_6kw != ist_6kw_ein:
        if not tydom_6kw and ist_6kw_ein:
            if schalt_kuerzlich:
                print("ℹ️  6kW AUS nach Schaltbefehl – kein 2h-Lock")
            else:
                bis_lokal = (datetime.utcnow() + timedelta(hours=2+CEST_OFFSET)).strftime("%H:%M")
                status["manuell_sperre_bis"] = (datetime.utcnow() + timedelta(hours=2)).isoformat()
                status["modus_6kw"] = None
                msg = f"🖐️ 6kW manuell ausgeschaltet – Automatik gesperrt bis {bis_lokal} Uhr"
                print(msg); meldungen.append(msg)
        else:
            print("ℹ️  6kW manuell EIN erkannt – Automatik laeuft weiter.")
        status["heizstab_6kw_ein"] = tydom_6kw

    ein_3kw = status.get("heizstab_3kw_ein", False)
    ein_6kw = status.get("heizstab_6kw_ein", False)

    laderate     = berechne_laderate(status.get("soc_verlauf", []))
    laderate_str = f"{laderate:.1f}%/h" if laderate is not None else "unbekannt"
    print(f"ℹ️  SOC={soc}% | Laderate={laderate_str} | Uebers.={ueberschuss}W | Netz={netzbezug}W")
    if not ist_6kw_saison():
        print("ℹ️  6kW: Sommersperre aktiv (16.Mai-30.Sep)")

    # ── 6kW AUSSCHALTEN ──────────────────────────────────────────────────────
    if ein_6kw:
        soll_aus = pruefe_6kw_ausschalten(daten, status)
        if soll_aus:
            if not status.get("ausschalt_pending_6kw"):
                status["ausschalt_pending_6kw"] = now_str
                print("⏳ 6kW AUS: Pending")
            elif ist_pending_bestaetigt(status.get("ausschalt_pending_6kw")):
                print("🔴 6kW wird AUSGESCHALTET")
                szenarien.append(SCN_AUS_6KW)
                erfasse_schaltpunkt(status, "6kw", "AUS", soc, pv_w)
                status["heizstab_6kw_ein"]      = False
                status["ausschalt_pending_6kw"] = None
                status["einschalt_pending_6kw"] = None
                if soc < 80:
                    status["soc_abschaltung_6kw"] = True
                status["modus_6kw"] = None
                schaltungen["aus_6kw"] += 1
                ein_6kw = False
                meldungen.append(
                    f"🔴 6kW Heizstab AUS\n"
                    f"SOC: {soc}% | Überschuss: {ueberschuss}W | Netz: {netzbezug}W")
        else:
            if status.get("ausschalt_pending_6kw"):
                status["ausschalt_pending_6kw"] = None
                print("ℹ️  6kW AUS Pending geloescht")

    # ── 3kW AUSSCHALTEN ──────────────────────────────────────────────────────
    if ein_3kw:
        soll_aus = pruefe_3kw_ausschalten(daten, status)
        if soll_aus:
            if not status.get("ausschalt_pending_3kw"):
                status["ausschalt_pending_3kw"] = now_str
                print("⏳ 3kW AUS: Pending")
            elif ist_pending_bestaetigt(status.get("ausschalt_pending_3kw")):
                print("🔴 3kW wird AUSGESCHALTET")
                szenarien.append(SCN_AUS_3KW)
                erfasse_schaltpunkt(status, "3kw", "AUS", soc, pv_w)
                status["heizstab_3kw_ein"]      = False
                status["ausschalt_pending_3kw"] = None
                status["einschalt_pending_3kw"] = None
                if soc < 70:
                    status["soc_abschaltung_3kw"] = True
                status["modus_3kw"] = None
                schaltungen["aus_3kw"] += 1
                ein_3kw = False
                meldungen.append(
                    f"🔴 3kW Heizstab AUS\n"
                    f"SOC: {soc}% | Überschuss: {ueberschuss}W | Netz: {netzbezug}W")
        else:
            if status.get("ausschalt_pending_3kw"):
                status["ausschalt_pending_3kw"] = None
                print("ℹ️  3kW AUS Pending geloescht")

    # ── 3kW EINSCHALTEN ──────────────────────────────────────────────────────
    if not ein_3kw:
        soll_ein, modus, sofort = pruefe_3kw_einschalten(daten, status, laderate)
        if soll_ein:
            if sofort:
                print("🟢 3kW sofort EIN (Einspeisung-Stopp)")
                szenarien.append(SCN_EIN_3KW)
                erfasse_schaltpunkt(status, "3kw", "EIN", soc, pv_w, modus)
                status["heizstab_3kw_ein"]      = True
                status["modus_3kw"]             = modus
                status["einschalt_pending_3kw"] = None
                status["soc_abschaltung_3kw"]   = False
                schaltungen["ein_3kw"] += 1
                meldungen.append(
                    f"🟢 3kW EIN (Einspeisung-Stopp)\n"
                    f"SOC: {soc}% | PV: {pv_w}W | "
                    f"Einspeisung: {daten.get('einspeisung_w',0)}W")
            else:
                if not status.get("einschalt_pending_3kw"):
                    status["einschalt_pending_3kw"] = now_str
                    print(f"⏳ 3kW EIN ({modus}): Pending")
                elif ist_pending_bestaetigt(status.get("einschalt_pending_3kw")):
                    print(f"🟢 3kW wird EINGESCHALTET ({modus})")
                    szenarien.append(SCN_EIN_3KW)
                    erfasse_schaltpunkt(status, "3kw", "EIN", soc, pv_w, modus)
                    status["heizstab_3kw_ein"]      = True
                    status["modus_3kw"]             = modus
                    status["einschalt_pending_3kw"] = None
                    status["soc_abschaltung_3kw"]   = False
                    schaltungen["ein_3kw"] += 1
                    meldungen.append(
                        f"🟢 3kW EIN ({modus})\n"
                        f"SOC: {soc}% | Laderate: {laderate_str} | Überschuss: {ueberschuss}W")
        else:
            if status.get("einschalt_pending_3kw"):
                status["einschalt_pending_3kw"] = None
                print("ℹ️  3kW EIN Pending geloescht")

    # ── 6kW EINSCHALTEN ──────────────────────────────────────────────────────
    if not ein_6kw:
        soll_ein, modus, sofort = pruefe_6kw_einschalten(daten, status, laderate)
        if soll_ein:
            if not status.get("einschalt_pending_6kw"):
                status["einschalt_pending_6kw"] = now_str
                print(f"⏳ 6kW EIN ({modus}): Pending")
            elif ist_pending_bestaetigt(status.get("einschalt_pending_6kw")):
                print(f"🟢 6kW wird EINGESCHALTET ({modus})")
                szenarien.append(SCN_EIN_6KW)
                erfasse_schaltpunkt(status, "6kw", "EIN", soc, pv_w, modus)
                status["heizstab_6kw_ein"]      = True
                status["modus_6kw"]             = modus
                status["einschalt_pending_6kw"] = None
                status["soc_abschaltung_6kw"]   = False
                schaltungen["ein_6kw"] += 1
                meldungen.append(
                    f"🟢 6kW EIN ({modus})\n"
                    f"SOC: {soc}% | Laderate: {laderate_str} | Überschuss: {ueberschuss}W")
        else:
            if status.get("einschalt_pending_6kw"):
                status["einschalt_pending_6kw"] = None
                print("ℹ️  6kW EIN Pending geloescht")

    return szenarien, meldungen


# ═══════════════════════════════════════════════════════════════════════════════
# TAGESDIAGRAMM
# ═══════════════════════════════════════════════════════════════════════════════
def erstelle_tagesdiagramm(status: dict) -> bytes:
    """
    Erstellt Tagesdiagramm als PNG (Sungrow-Stil) und gibt PNG-Bytes zurueck.
    Basiert auf erstelle_beispiel_diagramm.py – mit echten Messdaten.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        import matplotlib.patches as mpatches
        import matplotlib.lines as mlines
    except ImportError:
        print("❌ matplotlib nicht verfügbar – kein Diagramm")
        return None

    tages_verlauf    = status.get("tages_verlauf", [])
    schaltpunkte     = status.get("schaltpunkte_heute", [])
    datum_str        = status.get("tages_datum", lokal_jetzt().strftime("%Y-%m-%d"))
    schaltungen      = status.get("schaltungen_heute", {})
    ein3 = schaltungen.get("ein_3kw", 0)
    aus3 = schaltungen.get("aus_3kw", 0)
    ein6 = schaltungen.get("ein_6kw", 0)
    aus6 = schaltungen.get("aus_6kw", 0)

    # Zeitachse: Minuten seit Mitternacht
    def zeit_zu_min(zeit_str):
        try:
            h, m = map(int, zeit_str.split(":"))
            return h * 60 + m
        except Exception:
            return 0

    if tages_verlauf:
        x_min  = [zeit_zu_min(m["zeit"]) for m in tages_verlauf]
        pv_w   = [m.get("pv_w", 0)          for m in tages_verlauf]
        haus_w = [m.get("haus_w", 0)         for m in tages_verlauf]
        einsp  = [m.get("einspeisung_w", 0)  for m in tages_verlauf]
        netz   = [m.get("netzbezug_w", 0)    for m in tages_verlauf]
        bat_l  = [m.get("bat_laden_w", 0)    for m in tages_verlauf]
        bat_e  = [m.get("bat_entladen_w", 0) for m in tages_verlauf]
        soc    = [m.get("soc", 0)            for m in tages_verlauf]
        # Sungrow-Konvention: Batterie + = Entladen (oben), - = Laden (unten)
        bat_display  = [e - l for e, l in zip(bat_e, bat_l)]
        grid_display = [n - s for n, s in zip(netz, einsp)]
    else:
        # Keine Daten – leeres Diagramm
        x_min = [0, 1440]
        pv_w = haus_w = einsp = netz = bat_l = bat_e = soc = [0, 0]
        bat_display = grid_display = [0, 0]

    # Heizstab-Status aus schaltpunkten rekonstruieren
    def rekonstruiere_status(geraet: str) -> tuple:
        """Gibt (x_werte, y_werte) fuer Step-Plot zurueck."""
        punkte = [p for p in schaltpunkte if p["geraet"] == geraet]
        if not punkte:
            return [0, 1440], [0, 0]
        xs, ys = [0], [0]
        for p in sorted(punkte, key=lambda x: x["zeit"]):
            t = zeit_zu_min(p["zeit"])
            xs.append(t)
            ys.append(1 if p["aktion"] == "EIN" else 0)
        xs.append(1440)
        ys.append(ys[-1])
        return xs, ys

    x3, y3 = rekonstruiere_status("3kw")
    x6, y6 = rekonstruiere_status("6kw")

    # Y-Achsen-Grenzen dynamisch berechnen
    max_pos = max(max(pv_w, default=0), max(haus_w, default=0), 1000)
    max_neg = max(max(bat_l, default=0), max(einsp, default=0), 500)
    y_max   = max(max_pos * 1.15, 5000)
    y_min   = -max(max_neg * 1.15, 2000)

    # SOC-Achse: 0% auf Hoehe der 0W-Linie
    s_min = 100 * y_min / y_max

    DARK_GREEN = "#1a6e1a"
    DARK_BLUE  = "#003399"
    COL_PV     = "#FFA500"
    COL_BAT    = "#4CAF50"
    COL_GRID   = "#5B9BD5"
    COL_CONS   = "#b5a800"

    tick_idx    = list(range(0, 1441, 120))
    tick_labels = [f"{h:02d}:00" for h in range(0, 25, 2)]

    fig = plt.figure(figsize=(18, 14), facecolor="#f4f4f4")
    gs  = gridspec.GridSpec(3, 1, height_ratios=[5, 1.2, 1.2],
                            hspace=0.45, left=0.07, right=0.87,
                            top=0.95, bottom=0.05)

    # ── Hauptdiagramm ────────────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0])
    ax1.set_facecolor("#ffffff")
    axR = ax1.twinx()

    ax1.axhline(0, color="#888888", lw=1.2, zorder=3)

    if len(x_min) > 1:
        ax1.fill_between(x_min, 0, grid_display,
                         where=[v >= 0 for v in grid_display],
                         alpha=0.45, color=COL_GRID, interpolate=True)
        ax1.fill_between(x_min, 0, grid_display,
                         where=[v < 0 for v in grid_display],
                         alpha=0.45, color=COL_GRID, interpolate=True)
        ax1.plot(x_min, grid_display, color=COL_GRID, lw=0.6, alpha=0.5)

        ax1.fill_between(x_min, 0, bat_display,
                         where=[v >= 0 for v in bat_display],
                         alpha=0.45, color=COL_BAT, interpolate=True)
        ax1.fill_between(x_min, 0, bat_display,
                         where=[v < 0 for v in bat_display],
                         alpha=0.45, color=COL_BAT, interpolate=True)
        ax1.plot(x_min, bat_display, color="#2e7d32", lw=0.7, alpha=0.5)

        ax1.fill_between(x_min, 0, pv_w, alpha=0.35, color=COL_PV)
        ax1.plot(x_min, pv_w, color="#e67e00", lw=1.8)

        ax1.plot(x_min, haus_w, color=COL_CONS, lw=2.0, alpha=0.9, zorder=4)

        axR.plot(x_min, soc, color=DARK_GREEN, lw=3.0, zorder=6)

    axR.axhline(30,  color="red",      ls="--", lw=1.5, alpha=0.8, zorder=5)
    axR.axhline(70,  color=DARK_GREEN, ls=":",  lw=1.0, alpha=0.5, zorder=5)
    axR.axhline(80,  color=DARK_BLUE,  ls=":",  lw=1.0, alpha=0.5, zorder=5)
    axR.axhline(100, color="#aaaaaa",  ls=":",  lw=0.8, alpha=0.4, zorder=5)

    for y, txt, col in [(30, "30% Notreserve", "red"),
                        (70, "70% (3kW aus)",   DARK_GREEN),
                        (80, "80% (6kW aus)",   DARK_BLUE),
                        (100,"100%",             "#888888")]:
        axR.text(1440 + 10, y, txt, fontsize=7.5, color=col,
                 va="center", ha="left", clip_on=False)

    # Schaltpunkte auf SOC-Kurve einzeichnen
    def finde_soc_bei_zeit(zeit_str):
        t = zeit_zu_min(zeit_str)
        if not x_min:
            return 50
        idx = min(range(len(x_min)), key=lambda i: abs(x_min[i] - t))
        return soc[idx] if idx < len(soc) else 50

    for sp in schaltpunkte:
        t    = zeit_zu_min(sp["zeit"])
        s    = sp.get("soc", finde_soc_bei_zeit(sp["zeit"]))
        col  = DARK_GREEN if sp["geraet"] == "3kw" else DARK_BLUE
        kw   = "3kW" if sp["geraet"] == "3kw" else "6kW"
        mark = "o" if sp["aktion"] == "EIN" else "x"
        lw   = 1.2 if sp["aktion"] == "EIN" else 3.0
        axR.scatter([t], [s], color=col, s=130, zorder=10, marker=mark,
                    edgecolors="white" if sp["aktion"]=="EIN" else col,
                    linewidths=lw)
        dy = 6 if sp["aktion"] == "EIN" else -7
        axR.annotate(f"{kw} {sp['aktion']}", xy=(t, s),
                     xytext=(t, s + dy), fontsize=8, color=col,
                     ha="center", va="bottom" if dy > 0 else "top",
                     arrowprops=dict(arrowstyle="-", color=col, lw=0.8),
                     bbox=dict(boxstyle="round,pad=0.25", fc="white",
                               ec=col, alpha=0.85, lw=0.8))

    ax1.set_xlim(0, 1440)
    ax1.set_ylim(y_min, y_max)
    axR.set_ylim(s_min, 105)
    ax1.set_xticks(tick_idx)
    ax1.set_xticklabels(tick_labels, fontsize=9)
    ax1.set_ylabel("Leistung (W)", fontsize=10)
    axR.set_ylabel("Batterieladezustand (%)", fontsize=10, color=DARK_GREEN)
    axR.tick_params(axis="y", colors=DARK_GREEN)
    zyklus_text = (f"Schaltungen heute:  3kW (Brauchwasser) {ein3}× EIN / {aus3}× AUS"
                   f"   │   6kW (FBH) {ein6}× EIN / {aus6}× AUS")
    ax1.set_title(
        f"① Tagesbericht {datum_str} – PV Heizstab Automation\n{zyklus_text}",
        fontsize=12, fontweight="bold", pad=10)
    ax1.grid(True, alpha=0.2, linestyle="--")

    ax1.text(720, y_min * 0.35,
             "← Batterie lädt  /  Netz: Einspeisung →",
             ha="center", fontsize=8, color="#666666", style="italic")
    ax1.text(720, y_max * 0.05,
             "← Batterie entlädt  /  Netzbezug →",
             ha="center", fontsize=8, color="#666666", style="italic")

    legend_handles = [
        mpatches.Patch(color=COL_PV,   alpha=0.6, label="PV-Ertrag (W)"),
        mlines.Line2D ([],[],           color=COL_CONS, lw=2.2, label="Gesamtverbrauch (W)"),
        mpatches.Patch(color=COL_BAT,  alpha=0.6, label="Batterie (+ entladen / – laden)"),
        mpatches.Patch(color=COL_GRID, alpha=0.6, label="Netz (+ Bezug / – Einspeisung)"),
        mlines.Line2D ([],[],           color=DARK_GREEN, lw=2.8, label="Batterieladezustand (%)"),
        mlines.Line2D ([],[],           color=DARK_GREEN, marker="o", ms=8, ls="None",
                       markeredgecolor="white", label="3kW Einschalten ●"),
        mlines.Line2D ([],[],           color=DARK_GREEN, marker="x", ms=9, ls="None",
                       markeredgewidth=2.8, label="3kW Ausschalten ✕"),
        mlines.Line2D ([],[],           color=DARK_BLUE,  marker="o", ms=8, ls="None",
                       markeredgecolor="white", label="6kW Einschalten ●"),
        mlines.Line2D ([],[],           color=DARK_BLUE,  marker="x", ms=9, ls="None",
                       markeredgewidth=2.8, label="6kW Ausschalten ✕"),
    ]
    ax1.legend(handles=legend_handles, loc="upper left", fontsize=8.5,
               ncol=3, framealpha=0.92, edgecolor="#cccccc")

    # ── 3kW Heizstab Status ───────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1])
    ax2.set_facecolor("#ffffff")
    ax2.step(x3, y3, where="post", color=DARK_GREEN, lw=2.2)
    ax2.fill_between(x3, 0, y3, step="post", alpha=0.28, color=DARK_GREEN)

    ein3_punkte = [p for p in schaltpunkte if p["geraet"]=="3kw" and p["aktion"]=="EIN"]
    aus3_punkte = [p for p in schaltpunkte if p["geraet"]=="3kw" and p["aktion"]=="AUS"]
    for p in ein3_punkte:
        t = zeit_zu_min(p["zeit"])
        ax2.scatter([t], [1], color=DARK_GREEN, s=100, zorder=10,
                    marker="o", edgecolors="white", linewidths=1.2)
        ax2.text(t, 1.18, f"EIN\n{p['zeit']}", ha="center", fontsize=8.5,
                 color=DARK_GREEN, fontweight="bold")
    for p in aus3_punkte:
        t = zeit_zu_min(p["zeit"])
        ax2.scatter([t], [0], color=DARK_GREEN, s=120, zorder=10,
                    marker="x", linewidths=2.8)
        ax2.text(t, -0.3, f"AUS\n{p['zeit']}", ha="center", fontsize=8.5,
                 color=DARK_GREEN, fontweight="bold")

    ax2.set_xlim(0, 1440); ax2.set_ylim(-0.5, 1.8)
    ax2.set_yticks([0, 1])
    ax2.set_yticklabels(["AUS", "EIN"], fontsize=9, color=DARK_GREEN)
    ax2.set_xticks(tick_idx); ax2.set_xticklabels(tick_labels, fontsize=9)
    ax2.set_title("② 3kW Heizstab (Brauchwasser) – Schaltzustände",
                  fontsize=11, fontweight="bold", color=DARK_GREEN)
    ax2.grid(True, alpha=0.2, linestyle="--")
    ax2.axhline(0, color="#aaaaaa", lw=0.8)

    # ── 6kW Heizstab Status ───────────────────────────────────────────────────
    ax3 = fig.add_subplot(gs[2])
    ax3.set_facecolor("#ffffff")
    ax3.step(x6, y6, where="post", color=DARK_BLUE, lw=2.2)
    ax3.fill_between(x6, 0, y6, step="post", alpha=0.22, color=DARK_BLUE)

    ein6_punkte = [p for p in schaltpunkte if p["geraet"]=="6kw" and p["aktion"]=="EIN"]
    aus6_punkte = [p for p in schaltpunkte if p["geraet"]=="6kw" and p["aktion"]=="AUS"]
    for p in ein6_punkte:
        t = zeit_zu_min(p["zeit"])
        ax3.scatter([t], [1], color=DARK_BLUE, s=100, zorder=10,
                    marker="o", edgecolors="white", linewidths=1.2)
        ax3.text(t, 1.18, f"EIN\n{p['zeit']}", ha="center", fontsize=8.5,
                 color=DARK_BLUE, fontweight="bold")
    for p in aus6_punkte:
        t = zeit_zu_min(p["zeit"])
        ax3.scatter([t], [0], color=DARK_BLUE, s=120, zorder=10,
                    marker="x", linewidths=2.8)
        ax3.text(t, -0.3, f"AUS\n{p['zeit']}", ha="center", fontsize=8.5,
                 color=DARK_BLUE, fontweight="bold")

    ax3.set_xlim(0, 1440); ax3.set_ylim(-0.5, 1.8)
    ax3.set_yticks([0, 1])
    ax3.set_yticklabels(["AUS", "EIN"], fontsize=9, color=DARK_BLUE)
    ax3.set_xticks(tick_idx); ax3.set_xticklabels(tick_labels, fontsize=9)
    ax3.set_title("③ 6kW Heizstab (Fußbodenheizung) – Schaltzustände",
                  fontsize=11, fontweight="bold", color=DARK_BLUE)
    ax3.grid(True, alpha=0.2, linestyle="--")
    ax3.axhline(0, color="#aaaaaa", lw=0.8)

    # Schaltpunkte-Tabelle unterhalb des Diagramms
    sp_sorted = sorted(schaltpunkte, key=lambda x: x["zeit"])
    if sp_sorted:
        col_green = "#1a6e1a"
        col_blue  = "#003399"
        eintraege = []
        for sp in sp_sorted:
            kw  = "3kW" if sp["geraet"] == "3kw" else "6kW"
            col = col_green if sp["geraet"] == "3kw" else col_blue
            sym = "●" if sp["aktion"] == "EIN" else "✕"
            eintraege.append((f"{sp['zeit']}  {sym} {kw} {sp['aktion']}  SOC:{sp['soc']}%", col))
        # Mehrzeilig wenn viele Einträge
        zeilen = []
        zeile_parts = []
        zeile_cols  = []
        for txt, col in eintraege:
            zeile_parts.append(txt)
            zeile_cols.append(col)
        # Als eine Zeile ausgeben (genug Platz durch bbox_inches="tight")
        x_pos = 0.05
        y_pos = 0.008
        fig.text(0.02, y_pos, "Schaltpunkte:", fontsize=9, fontweight="bold",
                 color="#333333", transform=fig.transFigure)
        x_cursor = 0.18
        for txt, col in eintraege:
            fig.text(x_cursor, y_pos, txt, fontsize=9, color=col,
                     transform=fig.transFigure)
            x_cursor += len(txt) * 0.0065
            if x_cursor > 0.85:
                y_pos -= 0.018
                x_cursor = 0.18
    else:
        fig.text(0.5, 0.008, "Heute keine Schaltungen", ha="center",
                 fontsize=9, color="#888888", style="italic",
                 transform=fig.transFigure)

    buf = io.BytesIO()
    plt.savefig(buf, dpi=150, bbox_inches="tight", facecolor="#f4f4f4")
    plt.close()
    buf.seek(0)
    print("✅ Tagesdiagramm erstellt.")
    return buf.read()


# ═══════════════════════════════════════════════════════════════════════════════
# TAGESBERICHTE
# ═══════════════════════════════════════════════════════════════════════════════
def erstelle_morgenreport_text(status: dict) -> tuple:
    """Erstellt Morgen-Report Text. Returns (betreff, text)."""
    jetzt      = lokal_jetzt()
    datum_str  = jetzt.strftime("%d.%m.%Y")
    betreff    = f"PV Heizstab – Tages-Status {datum_str}"

    # Automations-Status prüfen
    ist_pausiert = False
    pause_info   = ""
    if os.path.exists(AUTOMATION_PAUSE_DATEI):
        try:
            with open(AUTOMATION_PAUSE_DATEI, "r", encoding="utf-8") as f:
                pause = json.load(f)
            pause_bis_str = pause.get("pause_bis", "")
            pause_bis = datetime.strptime(pause_bis_str, "%Y-%m-%d").date()
            if jetzt.date() <= pause_bis:
                ist_pausiert = True
                pause_info = f"Pausiert bis: {pause_bis.strftime('%d.%m.%Y')}  |  Grund: {pause.get('grund','?')}"
        except Exception:
            pass

    if ist_pausiert:
        status_zeile = f"⏸️  AUTOMATION PAUSIERT\n   {pause_info}"
    else:
        status_zeile = "✅  AUTOMATION LÄUFT NORMAL"

    text = f"""PV Heizstab – Tages-Status  {datum_str}

{'━' * 50}

{status_zeile}

{'━' * 50}

📋 ANLEITUNG – Automation pausieren / fortsetzen

AM PC (GitHub):
  1. {GITHUB_REPO_URL}
  2. Datei "automation_pause.json" öffnen → Bleistift-Symbol
  3. Inhalt ändern:
     PAUSIEREN:  {{"pause_bis": "2026-04-20", "grund": "Urlaub"}}
     FORTSETZEN: {{"pause_bis": "2000-01-01", "grund": ""}}
  4. "Commit changes" klicken

AM iPHONE (GitHub App):
  1. GitHub App öffnen
  2. Repository: sriedl271-glitch/pv-heizstab-automation
  3. Datei "automation_pause.json" antippen → Bleistift-Symbol
  4. Datum anpassen (Format: JJJJ-MM-TT)
     Beispiel 3 Tage:  {{"pause_bis": "{(jetzt + timedelta(days=3)).strftime('%Y-%m-%d')}", "grund": "Urlaub"}}
     Pause aufheben:   {{"pause_bis": "2000-01-01", "grund": ""}}
  5. "Commit changes" bestätigen

Datumsformat: JJJJ-MM-TT  (z.B. 2026-04-20 für 20. April 2026)

{'━' * 50}
Automatisch erstellt um 07:00 Uhr
"""
    return betreff, text


def erstelle_abendreport_text(status: dict, energie: dict) -> tuple:
    """Erstellt Abend-Report Text. Returns (betreff, text)."""
    jetzt      = lokal_jetzt()
    datum_str  = jetzt.strftime("%d.%m.%Y")
    betreff    = f"PV Heizstab – Tagesbericht {datum_str}"

    schaltungen  = status.get("schaltungen_heute", {})
    schaltpunkte = status.get("schaltpunkte_heute", [])
    soc_aktuell  = 0
    if status.get("tages_verlauf"):
        soc_aktuell = status["tages_verlauf"][-1].get("soc", 0)

    ein3 = schaltungen.get("ein_3kw", 0)
    aus3 = schaltungen.get("aus_3kw", 0)
    ein6 = schaltungen.get("ein_6kw", 0)
    aus6 = schaltungen.get("aus_6kw", 0)

    # Betriebsstunden berechnen aus Schaltpunkten
    def betriebsstunden(geraet: str) -> str:
        punkte = sorted([p for p in schaltpunkte if p["geraet"] == geraet],
                        key=lambda x: x["zeit"])
        if not punkte:
            return "0:00 Std."
        total_min = 0
        ein_zeit  = None
        for p in punkte:
            if p["aktion"] == "EIN":
                ein_zeit = p["zeit"]
            elif p["aktion"] == "AUS" and ein_zeit:
                def minu(z): h, m = map(int, z.split(":")); return h*60+m
                total_min += minu(p["zeit"]) - minu(ein_zeit)
                ein_zeit = None
        if ein_zeit:  # noch eingeschaltet
            total_min += jetzt.hour * 60 + jetzt.minute - (
                lambda z: int(z.split(":")[0])*60 + int(z.split(":")[1]))(ein_zeit)
        h, m = divmod(max(0, total_min), 60)
        return f"{h}:{m:02d} Std."

    std_3kw = betriebsstunden("3kw")
    std_6kw = betriebsstunden("6kw")

    # Schaltpunkte-Liste formatieren
    schalt_liste = ""
    for sp in sorted(schaltpunkte, key=lambda x: x["zeit"]):
        kw   = "3kW" if sp["geraet"] == "3kw" else "6kW"
        sym  = "🟢" if sp["aktion"] == "EIN" else "🔴"
        mod  = f" ({sp['modus']})" if sp.get("modus") else ""
        schalt_liste += (f"  {sp['zeit']} Uhr  {sym} {kw} {sp['aktion']}"
                         f"   SOC: {sp['soc']}% | PV: {sp['pv_w']}W{mod}\n")
    if not schalt_liste:
        schalt_liste = "  Heute keine Schaltungen\n"

    text = f"""PV Heizstab – Tagesbericht  {datum_str}

{'━' * 50}

🔋 BATTERIE
  Ladestand aktuell: {soc_aktuell}%

⚡ HEIZSTAB-STATISTIK
  3kW Brauchwasser:    {ein3}× EIN / {aus3}× AUS  |  {std_3kw} in Betrieb
  6kW Fußbodenheizung: {ein6}× EIN / {aus6}× AUS  |  {std_6kw} in Betrieb

⏰ SCHALTPUNKTE HEUTE
{schalt_liste}
☀️ ENERGIE HEUTE (geschätzt aus 5-Min-Messungen)
  PV-Erzeugung:        {energie.get('pv_kwh', 0.0):>6.1f} kWh
  Gesamtverbrauch:     {energie.get('haus_kwh', 0.0):>6.1f} kWh
  Netz-Einspeisung:    {energie.get('einspeisung_kwh', 0.0):>6.1f} kWh
  Netzbezug:           {energie.get('netzbezug_kwh', 0.0):>6.1f} kWh
  Batterie geladen:    {energie.get('bat_laden_kwh', 0.0):>6.1f} kWh
  Batterie entladen:   {energie.get('bat_entladen_kwh', 0.0):>6.1f} kWh

{'━' * 50}
Tagesdiagramm im Anhang  |  Erstellt um 21:00 Uhr
"""
    return betreff, text


def verarbeite_tagesberichte(status: dict) -> None:
    """Prueft ob Morgen- oder Abend-Report gesendet werden soll."""
    jetzt     = lokal_jetzt()
    heute_str = jetzt.strftime("%Y-%m-%d")
    now_utc_h = datetime.utcnow().hour
    now_utc_m = datetime.utcnow().minute

    # ── Morgen-Report: 07:00 CEST = 05:00 UTC ────────────────────────────────
    letzter_morgen = status.get("morgenreport_datum")
    morgen_faellig = (
        letzter_morgen is None or
        (letzter_morgen != heute_str
         and now_utc_h == MORGENREPORT_UTC_STUNDE
         and now_utc_m < REPORT_FENSTER_MIN)
    )
    if morgen_faellig:
        print("📬 Morgen-Report wird gesendet...")
        betreff, text = erstelle_morgenreport_text(status)
        sende_email(betreff, text)
        status["morgenreport_datum"] = heute_str
        print("✅ Morgen-Report gesendet.")

    # ── Abend-Report: 21:00 CEST = 19:00 UTC ─────────────────────────────────
    letzter_abend = status.get("abendreport_datum")
    abend_faellig = (
        letzter_abend is None or
        (letzter_abend != heute_str
         and now_utc_h == ABENDREPORT_UTC_STUNDE
         and now_utc_m < REPORT_FENSTER_MIN)
    )
    if abend_faellig:
        print("📊 Abend-Report wird erstellt...")
        # Offizielle iSolarCloud-Werte bevorzugen, Fallback auf 5-Min-Schätzung
        energie = status.get("tages_energie_isolarcloud") or berechne_tages_energie(
            status.get("tages_verlauf", []))
        quelle = "iSolarCloud" if status.get("tages_energie_isolarcloud") else "Schätzung"
        print(f"ℹ️  Energiedaten-Quelle: {quelle}")
        png_bytes = erstelle_tagesdiagramm(status)
        betreff, text = erstelle_abendreport_text(status, energie)
        sende_email_mit_anhang(betreff, text, png_bytes)
        status["abendreport_datum"] = heute_str
        print("✅ Abend-Report gesendet.")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main() -> None:
    print("=== PV MONITOR START ===")
    print(f"Zeit: {lokal_jetzt().strftime('%d.%m.%Y %H:%M:%S')} (CEST)")

    status = lade_status()

    # Neuer-Tag-Reset (Tagesdaten, Schaltpunkte)
    reset_wenn_neuer_tag(status)

    # iSolarCloud Daten holen
    app_key       = os.environ.get("ISOLARCLOUD_APP_KEY")
    secret_key    = os.environ.get("SOLARCLOUD_SECRET_KEY")
    user_account  = os.environ.get("ISOLARCLOUD_USER_ACCOUNT")
    user_password = os.environ.get("ISOLARCLOUD_USER_PASSWORD")

    if not all([app_key, secret_key, user_account, user_password]):
        print("❌ iSolarCloud Zugangsdaten fehlen!")
        return

    daten = hole_isolarcloud_daten(app_key, secret_key, user_account, user_password)
    if daten is None:
        print("❌ Keine iSolarCloud Daten – Abbruch.")
        speichere_status(status)
        return

    # SOC-Verlauf + Tages-Verlauf aktualisieren (immer, auch bei Pause)
    status["soc_verlauf"] = aktualisiere_soc_verlauf(status, daten["batterie_prozent"])
    aktualisiere_tages_verlauf(status, daten)

    # Offizielle iSolarCloud Tagesdaten speichern (für Abend-Report)
    if daten.get("tages_energie"):
        status["tages_energie_isolarcloud"] = daten["tages_energie"]

    # Tagesberichte prüfen (vor Pause-Check, damit Reports auch bei Pause kommen)
    verarbeite_tagesberichte(status)

    # Pausen prüfen
    if ist_automation_pausiert() or ist_manuell_pausiert(status):
        status["letzte_aktualisierung"] = datetime.utcnow().isoformat()
        speichere_status(status)
        print("=== PV MONITOR ENDE (Pause) ===")
        return

    # TYDOM Zugangsdaten prüfen
    tydom_email    = os.environ.get("TYDOM_EMAIL")
    tydom_passwort = os.environ.get("TYDOM_PASSWORD")
    if not tydom_email or not tydom_passwort:
        print("❌ TYDOM_EMAIL oder TYDOM_PASSWORD fehlen!")
        speichere_status(status)
        return

    # TYDOM: aktuellen Zustand lesen
    print("--- TYDOM Zustand lesen ---")
    tydom_zustand = tydom_ausfuehren(tydom_email, tydom_passwort)
    if tydom_zustand is None:
        print("❌ TYDOM nicht erreichbar – kein Schalten in diesem Zyklus.")
        status["letzte_aktualisierung"] = datetime.utcnow().isoformat()
        speichere_status(status)
        print("=== PV MONITOR ENDE ===")
        return

    # Schaltlogik anwenden
    print("--- Schaltlogik ---")
    szenarien, meldungen = verarbeite_schaltlogik(daten, status, tydom_zustand)

    # Szenarien ausführen
    if szenarien:
        print(f"--- TYDOM Schalten ({len(szenarien)} Szenario(en)) ---")
        tydom_ergebnis = tydom_ausfuehren(tydom_email, tydom_passwort, szenarien)
        status["letzte_schaltzeit"] = datetime.utcnow().isoformat()

        if tydom_ergebnis:
            print(f"ℹ️  TYDOM nach Schalten: "
                  f"3kW={'EIN' if tydom_ergebnis.get('3kw_ein') else 'AUS'}, "
                  f"6kW={'EIN' if tydom_ergebnis.get('6kw_ein') else 'AUS'} "
                  f"– Zustand wird nächsten Zyklus verifiziert")
        else:
            print("⚠️  TYDOM Bestätigungslesung fehlgeschlagen – Status bleibt wie gesetzt.")

        for meldung in meldungen:
            if "🖐️" not in meldung:
                benachrichtige("PV Heizstab", meldung)
    else:
        print("ℹ️  Kein Schalten erforderlich.")

    for meldung in meldungen:
        if "🖐️" in meldung:
            benachrichtige("PV Heizstab – Manuelle Aktion", meldung, prioritaet=1)

    status["letzte_aktualisierung"] = datetime.utcnow().isoformat()
    speichere_status(status)
    print("=== PV MONITOR ENDE ===")


if __name__ == "__main__":
    main()
