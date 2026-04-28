"""
PV-Heizstab-Automation – Hauptscript v2.9
TYDOM-Steuerung via PUT + Schaltlogik + Morgen-/Abend-Report mit Tagesdiagramm
v2.9: Saisonale Abschaltzeiten, Thermostat-Pause (Entlade-Betrieb), getrennte 6kW-Cutover-Zeit
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

# Pending-Mindestwartezeit: EIN 10 Min (2 Zyklen), AUS 10 Min (2 Zyklen)
PENDING_EIN_SEKUNDEN = 480   # EIN: 2 × 5 Min = 10 Min – Testwert (war 780 = 15 Min effektiv)
PENDING_AUS_SEKUNDEN = 480   # AUS: 2 × 5 Min = 10 Min – schneller Schutz bei fallendem SOC

# Zeitzone: CEST = UTC+2 (April–Oktober)
CEST_OFFSET = 2

# Berichtszeiten (UTC): 07:00 CEST = 05:00 UTC, 21:00 CEST = 19:00 UTC
MORGENREPORT_UTC_STUNDE = 5
ABENDREPORT_UTC_STUNDE  = 19
REPORT_FENSTER_MIN      = 10

# Betriebszeitfenster: System aktiv 06:00 – 22:00 Uhr CEST
BETRIEB_START_STUNDE  = 6   # 06:00 Uhr CEST
BETRIEB_ENDE_STUNDE   = 22  # 22:00 Uhr CEST
ABSCHALT_FENSTER_MIN  = 10  # Abschalt-Prüfung nur in den ersten 10 Min nach 22:00

# Hochspeicher-Entlade-Betrieb: Netzbezug-Grenzen
# Aktiv wenn: batterie_war_voll=True UND vor Cutover-Zeit
# Hochspeicher-Phase (SOC über AUS-Schwelle): tolerantere Grenze
# Pending-Fenster (SOC unter AUS-Schwelle): strengere Grenze
ENTLADE_NETZ_HOCHSPEICHER = 2000  # W – Netzbezug-Grenze in der Hochspeicher-Phase
ENTLADE_NETZ_NORMAL       = 800   # W – Netzbezug-Grenze im Pending-Fenster / untere SOC-Zone

# Thermostat-Pause: erkannte interne Thermostat-Abschaltung im Entlade-Betrieb
THERMOSTAT_PAUSE_MINUTEN = 20     # Minuten Pause nach Thermostat-Abschaltung (kein 2h-Lock)


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

def sende_email_mit_anhang(betreff: str, inhalt: str,
                           png_bytes: bytes = None,
                           png_bytes_2: bytes = None,
                           png_bytes_3: bytes = None) -> None:
    """Sendet E-Mail mit bis zu drei optionalen PNG-Anhängen.
    png_bytes   = Tagesdiagramm (aktueller Tag)
    png_bytes_2 = Regelübersichts-Diagramm
    png_bytes_3 = Laderate-Analyse Normalbetrieb
    """
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
    if png_bytes_2:
        img2 = MIMEImage(png_bytes_2, name="regeluebersicht.png")
        img2.add_header("Content-Disposition", "attachment", filename="regeluebersicht.png")
        msg.attach(img2)
    if png_bytes_3:
        img3 = MIMEImage(png_bytes_3, name="laderate_analyse.png")
        img3.add_header("Content-Disposition", "attachment", filename="laderate_analyse.png")
        msg.attach(img3)
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
        if gid not in (GERAET_3KW, GERAET_6KW):
            continue
        name = "3kW" if gid == GERAET_3KW else "6kW"
        for ep in geraet.get("endpoints", []):
            ep_id = ep.get("id")
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
        status["soc_verlauf"]        = []
        status["laderate_verlauf"]   = []
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

def ist_manuell_pausiert_3kw(status: dict) -> bool:
    sperre = status.get("manuell_sperre_3kw_bis")
    if not sperre:
        return False
    try:
        bis = datetime.fromisoformat(sperre)
        if datetime.utcnow() < bis:
            bis_lokal = bis + timedelta(hours=CEST_OFFSET)
            print(f"⏸️  3kW 2h-Sperre aktiv bis {bis_lokal.strftime('%H:%M')} Uhr")
            return True
        status["manuell_sperre_3kw_bis"] = None
    except Exception:
        status["manuell_sperre_3kw_bis"] = None
    return False


def ist_manuell_pausiert_6kw(status: dict) -> bool:
    sperre = status.get("manuell_sperre_6kw_bis")
    if not sperre:
        return False
    try:
        bis = datetime.fromisoformat(sperre)
        if datetime.utcnow() < bis:
            bis_lokal = bis + timedelta(hours=CEST_OFFSET)
            print(f"⏸️  6kW 2h-Sperre aktiv bis {bis_lokal.strftime('%H:%M')} Uhr")
            return True
        status["manuell_sperre_6kw_bis"] = None
    except Exception:
        status["manuell_sperre_6kw_bis"] = None
    return False

def ist_pending_bestaetigt(pending_seit: str, sekunden: int = PENDING_EIN_SEKUNDEN) -> bool:
    if not pending_seit:
        return False
    try:
        return (datetime.utcnow() - datetime.fromisoformat(pending_seit)).total_seconds() >= sekunden
    except Exception:
        return False

def lokal_minuten() -> int:
    """Minuten seit Mitternacht in CEST (0–1439)."""
    lokal = lokal_jetzt()
    return lokal.hour * 60 + lokal.minute

def get_cutover_minuten_6kw() -> int:
    """Cutover-Zeit für 6kW in Minuten seit Mitternacht CEST (Minute-Genauigkeit).
    Winter (Nov-Mär): 13:30 = 810 Min | Frühling/Herbst (Apr+Okt): 15:30 = 930 Min
    """
    monat = lokal_jetzt().month
    if monat in [11, 12, 1, 2, 3]:
        return 810   # 13:30
    return 930       # 15:30

def get_aus_pending_cutover() -> int:
    """Cutover-Stunde (CEST) für 3kW – ab hier AUS-Pending 10 Min.
    Winter (Nov-Mär): 15:00 | Frühling/Herbst (Apr+Okt): 17:00 | Sommer (Mai-Sep): 18:00
    """
    monat = lokal_jetzt().month
    if monat in [11, 12, 1, 2, 3]:
        return 15
    elif monat in [4, 10]:
        return 17
    else:
        return 18

def get_aus_pending_sekunden() -> int:
    """Gibt die aktuelle AUS-Pending-Zeit zurück (3kW-Cutover-Zeit).
    Vor Cutover: 10 Min (Testwert).
    Ab Cutover:  10 Min.
    """
    if lokal_jetzt().hour >= get_aus_pending_cutover():
        return PENDING_AUS_SEKUNDEN
    return PENDING_AUS_SEKUNDEN

def get_abschaltzeit_minuten_3kw() -> int:
    """Saisonale Abschaltzeit 3kW in Minuten seit Mitternacht CEST.
    Winter (Nov-Mär): 15:30=930 | Frühling/Herbst (Apr+Okt): 18:30=1110 | Sommer (Mai-Sep): 20:30=1230
    """
    monat = lokal_jetzt().month
    if monat in [11, 12, 1, 2, 3]:
        return 930   # 15:30
    elif monat in [4, 10]:
        return 1110  # 18:30
    else:
        return 1230  # 20:30

def get_abschaltzeit_minuten_6kw() -> int:
    """Saisonale Abschaltzeit 6kW in Minuten seit Mitternacht CEST.
    Winter (Nov-Mär): 14:30=870 | Frühling/Herbst (Apr+Okt): 16:30=990
    """
    monat = lokal_jetzt().month
    if monat in [11, 12, 1, 2, 3]:
        return 870   # 14:30
    return 990       # 16:30

def ist_entlade_betrieb(status: dict) -> bool:
    """True wenn Hochspeicher-Entlade-Betrieb aktiv (3kW):
    Batterie war heute mindestens einmal bei 100% UND wir sind vor der 3kW-Cutover-Zeit.
    """
    if not status.get("batterie_war_voll", False):
        return False
    return lokal_jetzt().hour < get_aus_pending_cutover()

def ist_entlade_betrieb_6kw(status: dict) -> bool:
    """True wenn Hochspeicher-Entlade-Betrieb für 6kW aktiv:
    Batterie war heute mindestens einmal bei 100% UND wir sind vor der 6kW-Cutover-Zeit (früher als 3kW).
    """
    if not status.get("batterie_war_voll", False):
        return False
    return lokal_minuten() < get_cutover_minuten_6kw()

def ist_thermostat_pausiert_3kw(status: dict) -> bool:
    """True wenn 3kW gerade in 20-Min-Thermostat-Pause (nur im Entlade-Betrieb gesetzt)."""
    pause = status.get("thermostat_pause_3kw_bis")
    if not pause:
        return False
    try:
        bis = datetime.fromisoformat(pause)
        if datetime.utcnow() < bis:
            bis_lokal = bis + timedelta(hours=CEST_OFFSET)
            print(f"⏸️  3kW Thermostat-Pause bis {bis_lokal.strftime('%H:%M')} Uhr")
            return True
        status["thermostat_pause_3kw_bis"] = None
    except Exception:
        status["thermostat_pause_3kw_bis"] = None
    return False

def ist_thermostat_pausiert_6kw(status: dict) -> bool:
    """True wenn 6kW gerade in 20-Min-Thermostat-Pause (nur im Entlade-Betrieb gesetzt)."""
    pause = status.get("thermostat_pause_6kw_bis")
    if not pause:
        return False
    try:
        bis = datetime.fromisoformat(pause)
        if datetime.utcnow() < bis:
            bis_lokal = bis + timedelta(hours=CEST_OFFSET)
            print(f"⏸️  6kW Thermostat-Pause bis {bis_lokal.strftime('%H:%M')} Uhr")
            return True
        status["thermostat_pause_6kw_bis"] = None
    except Exception:
        status["thermostat_pause_6kw_bis"] = None
    return False

def ist_betriebszeit() -> bool:
    """Prüft ob aktuell Betriebszeit (06:00 – 22:00 Uhr CEST) ist."""
    return BETRIEB_START_STUNDE <= lokal_jetzt().hour < BETRIEB_ENDE_STUNDE

def ist_abschaltzeitfenster() -> bool:
    """Erste 10 Minuten nach 22:00 Uhr – Abschalt-Prüfung wird durchgeführt."""
    lokal = lokal_jetzt()
    return lokal.hour == BETRIEB_ENDE_STUNDE and lokal.minute < ABSCHALT_FENSTER_MIN

def fuehre_abschalt_pruefung_durch(tydom_email: str, tydom_passwort: str, status: dict) -> None:
    """
    Abschalt-Prüfung um 22:00 Uhr (Betriebsende):
    Check 1: TYDOM Zustand lesen – falls EIN → Ausschalten + Pushover + E-Mail
    3 Minuten warten
    Check 2: Nochmals prüfen – falls immer noch EIN → Alarm + Notfall-Ausschalt-Versuch
    """
    print("🌙 Abschalt-Prüfung 22:00 Uhr – Check 1")
    zustand1 = tydom_ausfuehren(tydom_email, tydom_passwort)
    if zustand1 is None:
        print("❌ TYDOM nicht erreichbar – Abschalt-Prüfung fehlgeschlagen")
        msg = "⚠️ TYDOM nicht erreichbar um 22:00 Uhr – bitte Heizstäbe manuell prüfen!"
        benachrichtige("⚠️ PV Heizstab – Abschalt-Prüfung", msg, prioritaet=1)
        sende_email("⚠️ PV Heizstab – Abschalt-Prüfung fehlgeschlagen", msg)
        return

    ein_3kw = zustand1.get("3kw_ein", False)
    ein_6kw = zustand1.get("6kw_ein", False)
    szenarien_aus = []
    teile = []

    if ein_3kw:
        szenarien_aus.append(SCN_AUS_3KW)
        teile.append("3kW war EIN → wird ausgeschaltet")
        status["heizstab_3kw_ein"]       = False
        status["einschalt_schwelle_3kw"] = None
    if ein_6kw:
        szenarien_aus.append(SCN_AUS_6KW)
        teile.append("6kW war EIN → wird ausgeschaltet")
        status["heizstab_6kw_ein"]       = False
        status["einschalt_schwelle_6kw"] = None

    if szenarien_aus:
        tydom_ausfuehren(tydom_email, tydom_passwort, szenarien_aus)
        msg = "🌙 Abschalt-Prüfung 22:00 Uhr:\n" + "\n".join(teile)
        print(msg)
        benachrichtige("PV Heizstab – Abschaltung 22:00 Uhr", msg, prioritaet=1)
        sende_email("PV Heizstab – Abschaltung 22:00 Uhr", msg)
    else:
        print("✅ Abschalt-Prüfung Check 1: Beide Heizstäbe AUS – OK")

    # 3 Minuten warten vor Check 2
    print("⏳ Abschalt-Prüfung: 3 Minuten warten vor Check 2 ...")
    time.sleep(180)

    print("🌙 Abschalt-Prüfung 22:00 Uhr – Check 2")
    zustand2 = tydom_ausfuehren(tydom_email, tydom_passwort)
    if zustand2 is None:
        print("❌ TYDOM Check 2 nicht erreichbar")
        msg = "⚠️ TYDOM bei Abschalt-Check 2 nicht erreichbar – bitte manuell prüfen!"
        benachrichtige("⚠️ PV Heizstab – Check 2 fehlgeschlagen", msg, prioritaet=1)
        sende_email("⚠️ PV Heizstab – Abschalt-Check 2 fehlgeschlagen", msg)
        return

    alarm_teile = []
    notfall_szenarien = []
    if zustand2.get("3kw_ein", False):
        alarm_teile.append("3kW ist nach Check 2 immer noch EIN!")
        notfall_szenarien.append(SCN_AUS_3KW)
    if zustand2.get("6kw_ein", False):
        alarm_teile.append("6kW ist nach Check 2 immer noch EIN!")
        notfall_szenarien.append(SCN_AUS_6KW)

    if alarm_teile:
        alarm_msg = "⚠️ ACHTUNG – Abschalt-Prüfung 22:00 Uhr:\n" + "\n".join(alarm_teile)
        print(alarm_msg)
        benachrichtige("⚠️ PV Heizstab – ALARM Abschaltung", alarm_msg, prioritaet=1)
        sende_email("⚠️ PV Heizstab – Heizstab nach 22:00 immer noch EIN!", alarm_msg)
        # Letzter Notfall-Versuch
        tydom_ausfuehren(tydom_email, tydom_passwort, notfall_szenarien)
    else:
        print("✅ Abschalt-Prüfung Check 2: Beide Heizstäbe AUS – OK")


# ═══════════════════════════════════════════════════════════════════════════════
# SCHALTBEDINGUNGEN
# ═══════════════════════════════════════════════════════════════════════════════
def pruefe_3kw_einschalten(daten: dict, status: dict, laderate) -> tuple:
    """Returns (soll_ein, modus, sofort, einschalt_soc_min)
    einschalt_soc_min: SOC-Schwelle die das EIN ausgelöst hat (wird als AUS-Schwelle gespeichert)
    """
    soc         = daten["batterie_prozent"]
    pv          = daten["pv_leistung_w"]
    ueberschuss = daten["ueberschuss_w"]
    einspeisung = daten.get("einspeisung_w", 0)

    # ── Entlade-Betrieb (SOC heute erstmals 100% erreicht, vor Cutover) ──────
    if ist_entlade_betrieb(status):
        # Einspeisung-Stopp: nur im Entlade-Betrieb – sofort EIN ohne Pending
        if einspeisung > 0 and soc >= 93 and pv >= 1000:
            return True, "EINSPEISUNG_STOPP", True, None
        if soc >= 85 and pv >= 2000:
            return True, "HOCHSPEICHER", False, None
        return False, None, False, None

    # ── Normalbetrieb: PV-Überschuss + Ladegeschwindigkeit erforderlich ───────
    if laderate is None or laderate <= 0:
        return False, None, False, None

    # Nach-Ausschalt-Sperre: nach AUS unterhalb der EIN-Schwelle erst ab 75% neu einschalten
    if status.get("nach_ausschalt_sperre_3kw"):
        if laderate > 0 and soc >= 75 and ueberschuss >= 3000:
            return True, "NORMAL", False, 75
        return False, None, False, None

    if laderate >= 20 and 45 <= soc < 60 and ueberschuss >= 2000:
        return True, "NORMAL", False, 45
    if laderate >= 15 and 60 <= soc < 75 and ueberschuss >= 2000:
        return True, "NORMAL", False, 60
    if laderate > 0  and soc >= 75 and ueberschuss >= 2000:
        return True, "NORMAL", False, 75

    return False, None, False, None

def pruefe_3kw_ausschalten(daten: dict, status: dict) -> tuple:
    """Returns (soll_aus: bool, grund: str)"""
    soc                = daten["batterie_prozent"]
    ueberschuss        = daten["ueberschuss_w"]
    netzbezug          = daten["netzbezug_w"]
    pv                 = daten["pv_leistung_w"]
    modus              = status.get("modus_3kw", "NORMAL")
    einschalt_schwelle = status.get("einschalt_schwelle_3kw")

    # ── Hochspeicher-Entlade-Betrieb (batterie_war_voll=True, vor Cutover) ───
    if ist_entlade_betrieb(status):
        # Netzbezug-Grenze: toleranter in Hochspeicher-Phase, strenger im unteren SOC-Fenster
        netz_grenze = ENTLADE_NETZ_HOCHSPEICHER if soc >= 75 else ENTLADE_NETZ_NORMAL
        if netzbezug > netz_grenze:
            return True, f"Netz>{netz_grenze}W"
        # SOC-Grenze (PV-Bedingung entfällt – Batterie liefert Energie)
        if soc < 75:
            return True, "SOC<75%"
        # Einspeisung-Stopp: PV-Check bleibt aktiv, SOC-Check an neue Schwelle angepasst
        if modus == "EINSPEISUNG_STOPP" and (soc < 75 or pv < 1000):
            status["modus_3kw"] = "NORMAL"
            return False, ""
        return False, ""

    # ── Nach Cutover, batterie_war_voll=True → feste AUS-Schwelle 70% ─────────
    if status.get("batterie_war_voll", False):
        if netzbezug > 2000:
            return True, "Netz"
        if soc < 70:
            return True, "SOC<70%"
        return False, ""

    # ── Normalbetrieb ─────────────────────────────────────────────────────────
    if netzbezug > 2000:
        return True, "Netz"
    # Dynamische AUS-Schwelle: entspricht der SOC-Schwelle die das EIN ausgelöst hat
    if einschalt_schwelle is not None:
        if soc < einschalt_schwelle:
            return True, f"SOC<{einschalt_schwelle}%"
    else:
        if soc < 75:
            return True, "SOC<75%"
    if modus == "EINSPEISUNG_STOPP" and (soc < 75 or pv < 1000):
        status["modus_3kw"] = "NORMAL"
        return False, ""
    return False, ""

def pruefe_6kw_einschalten(daten: dict, status: dict, laderate) -> tuple:
    """Returns (soll_ein, modus, sofort, einschalt_soc_min)
    einschalt_soc_min: SOC-Schwelle die das EIN ausgelöst hat (wird als AUS-Schwelle gespeichert)
    """
    if not ist_6kw_saison():
        return False, None, False, None

    soc         = daten["batterie_prozent"]
    pv          = daten["pv_leistung_w"]
    ueberschuss = daten["ueberschuss_w"]

    # ── Entlade-Betrieb 6kW: nur Hochspeicher-Regel (vor 6kW-Cutover) ────────
    if ist_entlade_betrieb_6kw(status):
        if soc >= 90 and pv >= 4000:
            return True, "HOCHSPEICHER", False, None
        return False, None, False, None

    # ── Normalbetrieb: PV-Überschuss + Ladegeschwindigkeit erforderlich ───────
    if laderate is None or laderate <= 0:
        return False, None, False, None

    # Nach-Ausschalt-Sperre: nach AUS unterhalb der EIN-Schwelle erst ab 90% neu einschalten
    if status.get("nach_ausschalt_sperre_6kw"):
        if laderate > 0 and soc >= 90 and ueberschuss >= 4000:
            return True, "NORMAL", False, 90
        return False, None, False, None

    if laderate >= 20 and soc >= 75 and ueberschuss >= 4000:
        return True, "NORMAL", False, 75
    if laderate >= 15 and soc >= 83 and ueberschuss >= 4000:
        return True, "NORMAL", False, 83
    if laderate > 0  and soc >= 90 and ueberschuss >= 4000:
        return True, "NORMAL", False, 90

    return False, None, False, None

def pruefe_6kw_ausschalten(daten: dict, status: dict) -> tuple:
    """Returns (soll_aus: bool, grund: str)"""
    soc                = daten["batterie_prozent"]
    ueberschuss        = daten["ueberschuss_w"]
    netzbezug          = daten["netzbezug_w"]
    pv                 = daten["pv_leistung_w"]
    modus              = status.get("modus_6kw", "NORMAL")
    einschalt_schwelle = status.get("einschalt_schwelle_6kw")

    # ── Hochspeicher-Entlade-Betrieb 6kW (batterie_war_voll=True, vor 6kW-Cutover) ──
    if ist_entlade_betrieb_6kw(status):
        netz_grenze = ENTLADE_NETZ_HOCHSPEICHER if soc >= 80 else ENTLADE_NETZ_NORMAL
        if netzbezug > netz_grenze:
            return True, f"Netz>{netz_grenze}W"
        # SOC-Grenze (PV-Bedingung entfällt – Batterie liefert Energie)
        if soc < 80:
            return True, "SOC<80%"
        return False, ""

    # ── Nach Cutover, batterie_war_voll=True → feste AUS-Schwelle 80% ─────────
    if status.get("batterie_war_voll", False):
        if netzbezug > 2000:
            return True, "Netz"
        if soc < 80:
            return True, "SOC<80%"
        return False, ""

    # ── Normalbetrieb ─────────────────────────────────────────────────────────
    if netzbezug > 2000:
        return True, "Netz"
    if einschalt_schwelle is not None:
        if soc < einschalt_schwelle:
            return True, f"SOC<{einschalt_schwelle}%"
    else:
        if soc < 80:
            return True, "SOC<80%"
    return False, ""


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
        # Tageswechsel: alle Sperren zurücksetzen (frischer Start neuer Tag)
        status["nach_ausschalt_sperre_3kw"] = False
        status["nach_ausschalt_sperre_6kw"] = False
        status["einschalt_schwelle_3kw"]    = None
        status["einschalt_schwelle_6kw"]    = None
        status["batterie_war_voll"]         = False
        status["thermostat_pause_3kw_bis"]  = None
        status["thermostat_pause_6kw_bis"]  = None
        status["saisonale_abschaltung_3kw"] = None
        status["saisonale_abschaltung_6kw"] = None
        print("ℹ️  Tageswechsel: alle Einschalt-Sperren zurückgesetzt")
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
                print("ℹ️  3kW AUS nach Schaltbefehl – kein Lock")
            elif ist_entlade_betrieb(status):
                # Thermostat-Abschaltung im Entlade-Betrieb: 20-Min-Pause statt 2h-Lock
                pause_bis = (datetime.utcnow() + timedelta(minutes=THERMOSTAT_PAUSE_MINUTEN)).isoformat()
                status["thermostat_pause_3kw_bis"] = pause_bis
                status["modus_3kw"] = None
                bis_lokal = (datetime.utcnow() + timedelta(
                    minutes=THERMOSTAT_PAUSE_MINUTEN, hours=CEST_OFFSET)).strftime("%H:%M")
                msg = f"🌡️ 3kW Thermostat-Abschaltung erkannt – 20 Min Pause (bis {bis_lokal} Uhr)"
                print(msg)
                benachrichtige("PV Heizstab – Thermostat 3kW", msg)
            else:
                bis_lokal = (datetime.utcnow() + timedelta(hours=2+CEST_OFFSET)).strftime("%H:%M")
                status["manuell_sperre_3kw_bis"] = (datetime.utcnow() + timedelta(hours=2)).isoformat()
                status["modus_3kw"] = None
                msg = f"🖐️ 3kW manuell ausgeschaltet – Automatik gesperrt bis {bis_lokal} Uhr"
                print(msg); meldungen.append(msg)
        else:
            print("ℹ️  3kW manuell EIN erkannt – Automatik laeuft weiter.")
        status["heizstab_3kw_ein"] = tydom_3kw

    if tydom_6kw != ist_6kw_ein:
        if not tydom_6kw and ist_6kw_ein:
            if schalt_kuerzlich:
                print("ℹ️  6kW AUS nach Schaltbefehl – kein Lock")
            elif ist_entlade_betrieb_6kw(status):
                # Thermostat-Abschaltung im Entlade-Betrieb: 20-Min-Pause statt 2h-Lock
                pause_bis = (datetime.utcnow() + timedelta(minutes=THERMOSTAT_PAUSE_MINUTEN)).isoformat()
                status["thermostat_pause_6kw_bis"] = pause_bis
                status["modus_6kw"] = None
                bis_lokal = (datetime.utcnow() + timedelta(
                    minutes=THERMOSTAT_PAUSE_MINUTEN, hours=CEST_OFFSET)).strftime("%H:%M")
                msg = f"🌡️ 6kW Thermostat-Abschaltung erkannt – 20 Min Pause (bis {bis_lokal} Uhr)"
                print(msg)
                benachrichtige("PV Heizstab – Thermostat 6kW", msg)
            else:
                bis_lokal = (datetime.utcnow() + timedelta(hours=2+CEST_OFFSET)).strftime("%H:%M")
                status["manuell_sperre_6kw_bis"] = (datetime.utcnow() + timedelta(hours=2)).isoformat()
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

    # batterie_war_voll: einmalig setzen wenn SOC heute erstmals 100% erreicht
    if soc >= 100 and not status.get("batterie_war_voll", False):
        status["batterie_war_voll"] = True
        print("ℹ️  Batterie erstmals 100% – Hochspeicher-Entlade-Betrieb aktiviert (bis Cutover)")

    # Entlade-Betrieb: getrennt für 3kW (3kW-Cutover) und 6kW (6kW-Cutover, früher)
    entlade_betrieb     = ist_entlade_betrieb(status)
    entlade_betrieb_6kw = ist_entlade_betrieb_6kw(status)

    # AUS-Pending: im Entlade-Betrieb immer 10 Min (kein saisonales 20-Min-Pending)
    aus_pending_sek_3kw = PENDING_AUS_SEKUNDEN if entlade_betrieb     else get_aus_pending_sekunden()
    aus_pending_sek_6kw = PENDING_AUS_SEKUNDEN if entlade_betrieb_6kw else get_aus_pending_sekunden()
    if entlade_betrieb:
        print(f"ℹ️  Hochspeicher-Entlade-Betrieb aktiv 3kW (AUS-Pending: 10 Min)")
    if entlade_betrieb_6kw:
        print(f"ℹ️  Hochspeicher-Entlade-Betrieb aktiv 6kW (AUS-Pending: 10 Min)")

    # ── Saisonale Abschaltzeit prüfen ────────────────────────────────────────
    heute_str_lokal = lokal_jetzt().strftime("%Y-%m-%d")
    min_jetzt = lokal_minuten()

    if min_jetzt >= get_abschaltzeit_minuten_3kw():
        if status.get("saisonale_abschaltung_3kw") != heute_str_lokal:
            status["saisonale_abschaltung_3kw"] = heute_str_lokal
            if ein_3kw:
                szenarien.append(SCN_AUS_3KW)
                erfasse_schaltpunkt(status, "3kw", "AUS", soc, pv_w, "Abschaltzeit")
                status["heizstab_3kw_ein"]      = False
                status["modus_3kw"]             = None
                status["einschalt_pending_3kw"] = None
                status["ausschalt_pending_3kw"] = None
                schaltungen["aus_3kw"] += 1
                ein_3kw = False
                msg = f"🌙 3kW Abschaltzeit – Heizstab AUS bis morgen früh"
                print(msg); meldungen.append(msg)
            else:
                print("ℹ️  3kW Abschaltzeit erreicht – war bereits AUS")

    if ist_6kw_saison() and min_jetzt >= get_abschaltzeit_minuten_6kw():
        if status.get("saisonale_abschaltung_6kw") != heute_str_lokal:
            status["saisonale_abschaltung_6kw"] = heute_str_lokal
            if ein_6kw:
                szenarien.append(SCN_AUS_6KW)
                erfasse_schaltpunkt(status, "6kw", "AUS", soc, pv_w, "Abschaltzeit")
                status["heizstab_6kw_ein"]      = False
                status["modus_6kw"]             = None
                status["einschalt_pending_6kw"] = None
                status["ausschalt_pending_6kw"] = None
                schaltungen["aus_6kw"] += 1
                ein_6kw = False
                msg = f"🌙 6kW Abschaltzeit – Heizstab AUS bis morgen früh"
                print(msg); meldungen.append(msg)
            else:
                print("ℹ️  6kW Abschaltzeit erreicht – war bereits AUS")

    # ── 6kW AUSSCHALTEN ──────────────────────────────────────────────────────
    if ein_6kw:
        soll_aus, aus_grund_6kw = pruefe_6kw_ausschalten(daten, status)
        if soll_aus:
            if not status.get("ausschalt_pending_6kw"):
                status["ausschalt_pending_6kw"] = now_str
                print(f"⏳ 6kW AUS ({aus_grund_6kw}): Pending")
            elif ist_pending_bestaetigt(status.get("ausschalt_pending_6kw"), aus_pending_sek_6kw):
                print("🔴 6kW wird AUSGESCHALTET")
                szenarien.append(SCN_AUS_6KW)
                erfasse_schaltpunkt(status, "6kw", "AUS", soc, pv_w, aus_grund_6kw)
                status["heizstab_6kw_ein"]      = False
                status["ausschalt_pending_6kw"] = None
                status["einschalt_pending_6kw"] = None
                # Dynamische Einschalt-Sperre: falls SOC unter die EIN-Schwelle gefallen
                schwelle_6kw = status.get("einschalt_schwelle_6kw")
                if schwelle_6kw is not None and soc < schwelle_6kw:
                    status["nach_ausschalt_sperre_6kw"] = True
                    print(f"ℹ️  6kW Einschalt-Sperre aktiv: nächstes EIN erst ab 90% SOC")
                status["einschalt_schwelle_6kw"] = None
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
        soll_aus, aus_grund_3kw = pruefe_3kw_ausschalten(daten, status)
        if soll_aus:
            if not status.get("ausschalt_pending_3kw"):
                status["ausschalt_pending_3kw"] = now_str
                print(f"⏳ 3kW AUS ({aus_grund_3kw}): Pending")
            elif ist_pending_bestaetigt(status.get("ausschalt_pending_3kw"), aus_pending_sek_3kw):
                print("🔴 3kW wird AUSGESCHALTET")
                szenarien.append(SCN_AUS_3KW)
                erfasse_schaltpunkt(status, "3kw", "AUS", soc, pv_w, aus_grund_3kw)
                status["heizstab_3kw_ein"]      = False
                status["ausschalt_pending_3kw"] = None
                status["einschalt_pending_3kw"] = None
                # Dynamische Einschalt-Sperre: falls SOC unter die EIN-Schwelle gefallen
                schwelle_3kw = status.get("einschalt_schwelle_3kw")
                if schwelle_3kw is not None and soc < schwelle_3kw:
                    status["nach_ausschalt_sperre_3kw"] = True
                    print(f"ℹ️  3kW Einschalt-Sperre aktiv: nächstes EIN erst ab 75% SOC")
                status["einschalt_schwelle_3kw"] = None
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
    if not ein_3kw and not ist_manuell_pausiert_3kw(status):
        if status.get("saisonale_abschaltung_3kw") == heute_str_lokal:
            print("ℹ️  3kW Abschaltzeit – kein Einschalten bis morgen")
            if status.get("einschalt_pending_3kw"):
                status["einschalt_pending_3kw"] = None
        else:
            pause_3kw_war_gesetzt = bool(status.get("thermostat_pause_3kw_bis"))
            thermostat_3kw_aktiv  = ist_thermostat_pausiert_3kw(status)
            pause_3kw_abgelaufen  = pause_3kw_war_gesetzt and not thermostat_3kw_aktiv
            if thermostat_3kw_aktiv:
                if status.get("einschalt_pending_3kw"):
                    status["einschalt_pending_3kw"] = None
                    print("ℹ️  3kW EIN Pending geloescht (Thermostat-Pause)")
            else:
                soll_ein, modus, sofort, einschalt_soc_min_3kw = pruefe_3kw_einschalten(daten, status, laderate)
                # Nach Thermostat-Pause im Entlade-Betrieb: sofort EIN ohne Pending
                if soll_ein and pause_3kw_abgelaufen and entlade_betrieb:
                    sofort = True
                    modus  = modus or "HOCHSPEICHER"
                # Entlade-Betrieb: 10 Min Pending, sonst 20 Min
                ein_pending_3kw_sek = PENDING_AUS_SEKUNDEN if entlade_betrieb else PENDING_EIN_SEKUNDEN
                if soll_ein:
                    if sofort:
                        if pause_3kw_abgelaufen:
                            print("🟢 3kW sofort EIN (nach Thermostat-Pause)")
                            szenarien.append(SCN_EIN_3KW)
                            erfasse_schaltpunkt(status, "3kw", "EIN", soc, pv_w, modus)
                            status["heizstab_3kw_ein"]          = True
                            status["modus_3kw"]                 = modus
                            status["einschalt_pending_3kw"]     = None
                            status["einschalt_schwelle_3kw"]    = einschalt_soc_min_3kw
                            status["nach_ausschalt_sperre_3kw"] = False
                            schaltungen["ein_3kw"] += 1
                            msg = f"🟢 3kW EIN nach Thermostat-Pause\nSOC: {soc}% | PV: {pv_w}W"
                            print(msg)
                            benachrichtige("PV Heizstab – Thermostat-Pause 3kW beendet", msg)
                        else:
                            print("🟢 3kW sofort EIN (Einspeisung-Stopp)")
                            szenarien.append(SCN_EIN_3KW)
                            erfasse_schaltpunkt(status, "3kw", "EIN", soc, pv_w, modus)
                            status["heizstab_3kw_ein"]          = True
                            status["modus_3kw"]                 = modus
                            status["einschalt_pending_3kw"]     = None
                            status["einschalt_schwelle_3kw"]    = einschalt_soc_min_3kw
                            status["nach_ausschalt_sperre_3kw"] = False
                            schaltungen["ein_3kw"] += 1
                            meldungen.append(
                                f"🟢 3kW EIN (Einspeisung-Stopp)\n"
                                f"SOC: {soc}% | PV: {pv_w}W | "
                                f"Einspeisung: {daten.get('einspeisung_w',0)}W")
                    else:
                        if not status.get("einschalt_pending_3kw"):
                            status["einschalt_pending_3kw"] = now_str
                            print(f"⏳ 3kW EIN ({modus}): Pending")
                        elif ist_pending_bestaetigt(status.get("einschalt_pending_3kw"), ein_pending_3kw_sek):
                            print(f"🟢 3kW wird EINGESCHALTET ({modus})")
                            szenarien.append(SCN_EIN_3KW)
                            erfasse_schaltpunkt(status, "3kw", "EIN", soc, pv_w, modus)
                            status["heizstab_3kw_ein"]          = True
                            status["modus_3kw"]                 = modus
                            status["einschalt_pending_3kw"]     = None
                            status["einschalt_schwelle_3kw"]    = einschalt_soc_min_3kw
                            status["nach_ausschalt_sperre_3kw"] = False
                            schaltungen["ein_3kw"] += 1
                            meldungen.append(
                                f"🟢 3kW EIN ({modus})\n"
                                f"SOC: {soc}% | Laderate: {laderate_str} | Überschuss: {ueberschuss}W")
                else:
                    if status.get("einschalt_pending_3kw"):
                        status["einschalt_pending_3kw"] = None
                        print("ℹ️  3kW EIN Pending geloescht")

    # ── 6kW EINSCHALTEN ──────────────────────────────────────────────────────
    if not ein_6kw and not ist_manuell_pausiert_6kw(status):
        if status.get("saisonale_abschaltung_6kw") == heute_str_lokal:
            print("ℹ️  6kW Abschaltzeit – kein Einschalten bis morgen")
            if status.get("einschalt_pending_6kw"):
                status["einschalt_pending_6kw"] = None
        else:
            pause_6kw_war_gesetzt = bool(status.get("thermostat_pause_6kw_bis"))
            thermostat_6kw_aktiv  = ist_thermostat_pausiert_6kw(status)
            pause_6kw_abgelaufen  = pause_6kw_war_gesetzt and not thermostat_6kw_aktiv
            if thermostat_6kw_aktiv:
                if status.get("einschalt_pending_6kw"):
                    status["einschalt_pending_6kw"] = None
                    print("ℹ️  6kW EIN Pending geloescht (Thermostat-Pause)")
            else:
                soll_ein, modus, sofort, einschalt_soc_min_6kw = pruefe_6kw_einschalten(daten, status, laderate)
                # Nach Thermostat-Pause im Entlade-Betrieb 6kW: sofort EIN ohne Pending
                sofort_6kw = pause_6kw_abgelaufen and entlade_betrieb_6kw and soll_ein
                # Entlade-Betrieb oder Langsames Laden (SOC≥90): 10 Min Pending, sonst 20 Min
                ein_pending_6kw_sek = PENDING_AUS_SEKUNDEN if (entlade_betrieb_6kw or einschalt_soc_min_6kw == 90) else PENDING_EIN_SEKUNDEN
                if soll_ein:
                    if sofort_6kw:
                        print("🟢 6kW sofort EIN (nach Thermostat-Pause)")
                        szenarien.append(SCN_EIN_6KW)
                        erfasse_schaltpunkt(status, "6kw", "EIN", soc, pv_w, modus)
                        status["heizstab_6kw_ein"]          = True
                        status["modus_6kw"]                 = modus
                        status["einschalt_pending_6kw"]     = None
                        status["einschalt_schwelle_6kw"]    = einschalt_soc_min_6kw
                        status["nach_ausschalt_sperre_6kw"] = False
                        schaltungen["ein_6kw"] += 1
                        msg = f"🟢 6kW EIN nach Thermostat-Pause\nSOC: {soc}% | PV: {pv_w}W"
                        print(msg)
                        benachrichtige("PV Heizstab – Thermostat-Pause 6kW beendet", msg)
                    else:
                        if not status.get("einschalt_pending_6kw"):
                            status["einschalt_pending_6kw"] = now_str
                            print(f"⏳ 6kW EIN ({modus}): Pending")
                        elif ist_pending_bestaetigt(status.get("einschalt_pending_6kw"), ein_pending_6kw_sek):
                            print(f"🟢 6kW wird EINGESCHALTET ({modus})")
                            szenarien.append(SCN_EIN_6KW)
                            erfasse_schaltpunkt(status, "6kw", "EIN", soc, pv_w, modus)
                            status["heizstab_6kw_ein"]          = True
                            status["modus_6kw"]                 = modus
                            status["einschalt_pending_6kw"]     = None
                            status["einschalt_schwelle_6kw"]    = einschalt_soc_min_6kw
                            status["nach_ausschalt_sperre_6kw"] = False
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
# REGELÜBERSICHTS-DIAGRAMM
# ═══════════════════════════════════════════════════════════════════════════════
def erstelle_regeldiagramm() -> bytes:
    """
    Erstellt Regelübersichts-Diagramm v6 als PNG.
    X-Achse: 05:00 – 23:00 Uhr CEST (Dezimalstunden)
    Y-Achse: SOC 0% – 100%
    Links (bis 14:00): 3kW Brauchwasser-Zonen
    Rechts (ab 14:00): 6kW Fussboden-Zonen
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.lines as mlines
        import matplotlib.ticker as mticker
        import numpy as np
    except ImportError:
        print("❌ matplotlib nicht verfügbar – kein Regeldiagramm")
        return None

    DARK_GREEN  = "#1a6e1a"
    DARK_ORANGE = "#cc5500"

    # Schwellenwerte
    S3_SCHNELL  = 45
    S3_MITTEL   = 60
    S3_SPERRE   = 75  # identisch mit S3_LANGSAM nach Redesign (nach_ausschalt_sperre → 75%)
    S3_LANGSAM  = 75
    S3_ABSCHALT = 85
    S3_HOCH     = 93
    S3_EINSP    = 99
    S6_SCHNELL  = 75
    S6_MITTEL   = 83
    S6_LANGSAM  = 90
    S6_HOCH     = 98

    X_MIN, X_MID, X_MAX = 5.0, 14.0, 23.0

    fig, ax = plt.subplots(figsize=(23, 13))
    plt.subplots_adjust(left=0.175, right=0.875, top=0.875, bottom=0.22)
    ax.set_xlim(X_MIN, X_MAX)
    ax.set_ylim(-3, 105)
    ax.set_facecolor("#f8f8f8")

    def zone_rect(x0, x1, y0, y1, fc, alpha=1.0):
        ax.fill_betweenx([y0, y1], x0, x1,
                         facecolor=fc, alpha=alpha, edgecolor="none", zorder=0)

    # ── Zonen ────────────────────────────────────────────────────────────────
    # 0–30% volle Breite rot
    zone_rect(X_MIN, X_MAX,  0,           30,          "#ffbbbb")
    # 3kW linke Haelfte (ab 30%)
    zone_rect(X_MIN, X_MID, 30,           S3_SCHNELL,  "#fff5c0")
    zone_rect(X_MIN, X_MID, S3_SCHNELL,   S3_MITTEL,   "#b8f0b8")
    zone_rect(X_MIN, X_MID, S3_MITTEL,    S3_SPERRE,   "#80d880")
    zone_rect(X_MIN, X_MID, S3_SPERRE,    S3_LANGSAM,  "#50c050")
    zone_rect(X_MIN, X_MID, S3_LANGSAM,   S3_ABSCHALT, "#38a838")
    zone_rect(X_MIN, X_MID, S3_ABSCHALT,  S3_HOCH,     "#a8e8a8")
    zone_rect(X_MIN, X_MID, S3_HOCH,      S3_EINSP,    "#ffff88")
    zone_rect(X_MIN, X_MID, S3_EINSP,     100,         "#ffe000")
    # 6kW rechte Haelfte (ab 30%)
    zone_rect(X_MID, X_MAX, 30,           S6_SCHNELL,  "#ffd0a0")
    zone_rect(X_MID, X_MAX, S6_SCHNELL,   S6_MITTEL,   "#ffb870")
    zone_rect(X_MID, X_MAX, S6_MITTEL,    S6_LANGSAM,  "#ff9040")
    zone_rect(X_MID, X_MAX, S6_LANGSAM,   S6_HOCH,     "#ff6820")
    zone_rect(X_MID, X_MAX, S6_HOCH,      100,         "#ffff88")

    # ── Notreserve-Text ──────────────────────────────────────────────────────
    ax.text((X_MIN + X_MAX) / 2, 15,
            "Stromausfall – 0-30% Notstromreserve – Kein EIN",
            fontsize=10, ha="center", va="center",
            color="#880000", fontweight="bold", zorder=3)

    # ── Ausserhalb Betrieb (05:00–06:00) ─────────────────────────────────────
    zone_rect(X_MIN, 6.0, -3, 105, "#888888", alpha=0.08)
    ax.text(5.5, 50, "außerhalb\nBetrieb", fontsize=7, ha="center", va="center",
            color="#888888", rotation=90)

    # ── 30%-Linie volle Breite ───────────────────────────────────────────────
    ax.plot([X_MIN, X_MAX], [30, 30], color="red", ls="--", lw=1.5, alpha=0.8, zorder=2)

    # ── 3kW Schwellenlinien (linke Haelfte) ───────────────────────────────────
    for soc_val, ls, lw in [
            (S3_SCHNELL,  "--", 0.8),
            (S3_MITTEL,   "--", 0.8),
            (S3_SPERRE,   "-.", 0.8),
            (S3_LANGSAM,  "-",  1.3),
            (S3_ABSCHALT, "--", 1.0),
            (S3_HOCH,     "-",  1.5),
            (S3_EINSP,    "-",  0.8)]:
        ax.plot([X_MIN, X_MID], [soc_val, soc_val],
                color=DARK_GREEN, ls=ls, lw=lw, alpha=0.65, zorder=2)

    # ── 6kW Schwellenlinien (rechte Haelfte) ──────────────────────────────────
    for soc_val, ls, lw in [
            (S6_SCHNELL, "-",  1.3),
            (S6_MITTEL,  "--", 0.8),
            (S6_LANGSAM, "--", 0.8),
            (S6_HOCH,    "-",  1.5)]:
        ax.plot([X_MID, X_MAX], [soc_val, soc_val],
                color=DARK_ORANGE, ls=ls, lw=lw, alpha=0.65, zorder=2)

    # ── Vertikale Trennlinien ─────────────────────────────────────────────────
    ax.axvline(X_MID, color="#555555", lw=1.5, alpha=0.35, zorder=3)
    ax.axvline(6.0,   color="#222222", lw=2.5, zorder=5)
    ax.axvline(22.0,  color="#222222", lw=2.5, zorder=5)

    # ── Betrieb Start/Ende (oberhalb Rahmen) ──────────────────────────────────
    ax.text(6.0,  107, "Betrieb\nStart", fontsize=8, ha="center", va="bottom",
            fontweight="bold", color="#222222", clip_on=False)
    ax.text(22.0, 107, "Betrieb\nEnde",  fontsize=8, ha="center", va="bottom",
            fontweight="bold", color="#222222", clip_on=False)

    # ── Cutover-Linien + Texte (im orangenen Bereich) ─────────────────────────
    cutover_data = [
        (16.0, "#1155cc", "16:00 Cutover Winter (Nov.–März)"),
        (17.0, "#7722aa", "17:00 Cutover Frühling/Herbst (Apr.+Okt.)"),
        (18.0, "#bb4400", "18:00 Cutover Sommer (Mai–Sep.)"),
    ]
    for xc, col, lbl in cutover_data:
        ax.axvline(xc, color=col, lw=1.5, ls="--", alpha=0.9, zorder=4)
        ax.text(xc - 0.25, 33, lbl, fontsize=7.5, ha="center", va="bottom",
                color=col, rotation=90, fontweight="bold", zorder=6)

    # ── Beispiel SOC-Kurve (Mustertagsverlauf) ────────────────────────────────
    soc_x = np.array([6.0, 7.0, 8.0,  9.0,  10.0, 10.5,
                      12.0, 14.0, 15.0, 16.0, 17.5, 19.0, 21.0, 23.0])
    soc_y = np.array([62,  72,  82,   91,   97,   100,
                      100,  100,  96,   89,   79,   68,   57,   47])
    ax.plot(soc_x, soc_y, color="#2244cc", lw=2.5, zorder=10, clip_on=True)
    ax.text(5.3, 68, "SOC-Verlauf (Beispiel)", fontsize=7.5,
            color="#2244cc", va="bottom", ha="left")

    # Stern bei erstem SOC=100%
    first_100_idx = int(np.argmax(soc_y >= 100))
    x_star = float(soc_x[first_100_idx])   # = 10.5
    ax.plot(x_star, 100.0, marker="*", markersize=18, color="#cc8800",
            markeredgecolor="#884400", zorder=12, clip_on=False)
    ax.annotate(
        "batterie_war_voll=True\nab 10:30 → Entlade-Betrieb aktiv",
        xy=(x_star, 100.0), xytext=(x_star + 0.7, 102.0),
        fontsize=8, ha="left", va="center", zorder=9,
        bbox=dict(boxstyle="round,pad=0.45", facecolor="#fffde0",
                  edgecolor="#cc8800", alpha=0.95, lw=1.4),
        color="#663300",
        arrowprops=dict(arrowstyle="->", color="#cc8800", lw=1.5,
                        connectionstyle="arc3,rad=-0.25"))

    # ── Entlade-Betrieb Box ───────────────────────────────────────────────────
    ax.annotate(
        "ENTLADE-BETRIEB\n"
        "(aktiv: batterie_war_voll=True\n"
        "UND vor Cutover-Zeit)\n"
        "EIN 3kW: nur SOC ≥93% + PV ≥2.000W\n"
        "EIN 6kW: nur SOC ≥98% + PV ≥4.500W\n"
        "Alle Normal-Regeln BLOCKIERT\n"
        "AUS 3kW: SOC < 85% oder Netz >\n"
        "AUS 6kW: SOC < 93% oder Netz >\n"
        "Netz ≤ 2.000W (SOC über Grenze)\n"
        "Netz ≤ 800W (SOC unter Grenze)\n"
        "EIN-Pending: 10 Min (480s)",
        xy=(X_MID, 80), fontsize=8, ha="center", va="center", zorder=8,
        bbox=dict(boxstyle="round,pad=0.5", facecolor="#ffeedd",
                  edgecolor="#cc6600", alpha=0.95, lw=1.3),
        color="#883300")

    # ── Pending Box (rechts im orangenen Bereich) ─────────────────────────────
    ax.annotate(
        "PENDING-ZEITEN\n"
        "EIN Normal (außer 6kW Langsam): 20 Min\n"
        "EIN 6kW Langsames Laden (≥90%): 10 Min\n"
        "EIN Entlade-Betrieb (beide): 10 Min\n"
        "AUS vor Cutover: 20 Min\n"
        "AUS ab Cutover: 10 Min\n"
        "AUS Entlade-Betrieb: immer 10 Min",
        xy=(21.5, 55), fontsize=8, ha="center", va="center", zorder=8,
        bbox=dict(boxstyle="round,pad=0.5", facecolor="#eeeeff",
                  edgecolor="#3355aa", alpha=0.95, lw=1.2),
        color="#002299")

    # ── Betriebszeit-Pfeil ────────────────────────────────────────────────────
    ax.annotate("", xy=(22.0, -2), xytext=(6.0, -2),
                arrowprops=dict(arrowstyle="<->", color="#333333", lw=1.5))
    ax.text(14.0, -2, "  Betriebszeit 06:00 – 22:00 Uhr CEST  ",
            fontsize=8.5, ha="center", va="center", color="#333333",
            bbox=dict(fc="white", ec="none", pad=1))

    # ── Header (weisser Bereich bei y=103) ───────────────────────────────────
    ax.text((X_MIN + X_MID) / 2, 103, "← 3kW Brauchwasser",
            fontsize=15, ha="center", va="center",
            fontweight="bold", color=DARK_GREEN, clip_on=True)
    ax.text((X_MID + X_MAX) / 2, 103, "6kW Fußbodenheizung →",
            fontsize=15, ha="center", va="center",
            fontweight="bold", color=DARK_ORANGE, clip_on=True)

    # ── 3kW Zonenbeschriftungen (schwarz, einzeilig, ab x=6.15) ──────────────
    labels_3kw = [
        (37.5, "3kW: kein EIN (30–44%: SOC zu niedrig)"),
        (52.5, "3kW EIN: Schnelles Laden – Laderate ≥20%/h, SOC ≥45%, Üb. ≥3.200W | Nach Sperre: kein EIN bis ≥75%"),
        (65.0, "3kW EIN: Mittleres Laden – Laderate ≥15%/h, SOC ≥60%, Üb. ≥3.200W | Nach Sperre: kein EIN bis ≥75%"),
        (80.0, "3kW EIN: Langsames Laden – Laderate > 0, SOC ≥75%, Üb. ≥3.200W (auch nach Ausschalt-Sperre)"),
        (89.0, "Normalbetrieb: Langsames Laden aktiv | Entlade-Betrieb: kein EIN (Normal-Regeln BLOCKIERT)"),
        (96.0, "3kW EIN: HOCHSPEICHER (nur Entlade-Betrieb) – SOC ≥93% + PV ≥2.000W – 10 Min Pending"),
        (99.5, "3kW EIN: EINSPEISUNG_STOPP (beide Modi) – SOC ≥99%, sofort"),
    ]
    for y, txt in labels_3kw:
        ax.text(6.15, y, txt, fontsize=7.5, color="black",
                va="center", ha="left", zorder=5, clip_on=True)

    # ── 6kW Zonenbeschriftungen (schwarz, einzeilig, bei x=21.85) ─────────────
    labels_6kw = [
        (52.5, "6kW: kein EIN zwischen 30–74% SOC | Nach Sperre: kein EIN bis ≥90%"),
        (79.0, "6kW EIN: Schnelles Laden – Laderate ≥20%/h, SOC ≥75%, Üb. ≥6.300W | Nach Sperre: kein EIN bis ≥90%"),
        (86.5, "6kW EIN: Mittleres Laden – Laderate ≥15%/h, SOC ≥83%, Üb. ≥6.300W | Nach Sperre: kein EIN bis ≥90%"),
        (94.0, "6kW EIN: Langsames Laden – Laderate > 0, SOC ≥90%, Üb. ≥6.300W – 10 Min Pending (auch nach Sperre)"),
        (99.0, "6kW EIN: HOCHSPEICHER (nur Entlade-Betrieb) – SOC ≥98% + PV ≥4.500W – 10 Min Pending"),
    ]
    for y, txt in labels_6kw:
        ax.text(21.85, y, txt, fontsize=7.5, color="black",
                va="center", ha="right", zorder=5, clip_on=True)

    # ── 3kW Schwellenbeschriftungen (AUSSEN LINKS bei x=4.45) ────────────────
    left_schwellen = [
        (S3_EINSP,    DARK_GREEN, "3kW: 99% Einsp.-Stopp"),
        (S3_HOCH,     DARK_GREEN, "3kW: 93% Hochsp.-EIN"),
        (S3_ABSCHALT, DARK_GREEN, "3kW: 85% Hochsp.-AUS"),
        (S3_LANGSAM,  DARK_GREEN, "3kW: 75% Langsam / Sperre / AUS"),
        (S3_MITTEL,   DARK_GREEN, "3kW: 60% Mittel"),
        (S3_SCHNELL,  DARK_GREEN, "3kW: 45% Schnell"),
        (30,          "red",      "30% Notstromreserve"),
    ]
    for soc_val, col, lbl in left_schwellen:
        ax.text(4.45, soc_val, lbl, fontsize=7, color=col,
                va="center", ha="right", clip_on=False, zorder=6)

    # ── 6kW Schwellenbeschriftungen (AUSSEN RECHTS bei x=23.08) ──────────────
    right_schwellen = [
        (S6_HOCH,    DARK_ORANGE, "6kW: 98% Hochsp.-EIN"),
        (S6_LANGSAM, DARK_ORANGE, "6kW: 90% Langsam"),
        (S6_MITTEL,  DARK_ORANGE, "6kW: 83% Mittel"),
        (S6_SCHNELL, DARK_ORANGE, "6kW: 75% Schnell"),
    ]
    for soc_val, col, lbl in right_schwellen:
        ax.text(23.08, soc_val, lbl, fontsize=7, color=col,
                va="center", ha="left", clip_on=False, zorder=6)

    # ── Achsen ────────────────────────────────────────────────────────────────
    ax.set_xticks([h for h in range(5, 24)])
    ax.set_xticklabels([f"{h:02d}:00" for h in range(5, 24)], fontsize=9)
    ax.set_yticks(range(0, 105, 5))
    ax.yaxis.set_minor_locator(mticker.MultipleLocator(1))
    ax.set_ylabel("Batterieladezustand SOC (%)", fontsize=10, y=0.15)
    ax.set_xlabel("Uhrzeit (CEST)", fontsize=10, labelpad=15)
    ax.grid(True, alpha=0.15, linestyle="--", which="major")
    ax.grid(True, alpha=0.06, linestyle=":",  which="minor")

    ax.set_title(
        "PV Heizstab Automation – Regelübersicht v2.6 (Stand: 18.04.2026)\n"
        "Links (bis 14:00): 3kW Brauchwasser  |  Rechts (ab 14:00): 6kW Fußbodenheizung\n"
        "Normalbetrieb: Schnell/Mittel/Langsam (Überschuss+Laderate)  |  Entlade-Betrieb: nur Hochspeicher-Regel",
        fontsize=11, fontweight="bold", pad=10)

    # ── Legende ───────────────────────────────────────────────────────────────
    legend_handles = [
        mpatches.Patch(color=DARK_GREEN,  alpha=0.7,
                       label="3kW Brauchwasser – EIN-Zonen (grün)"),
        mpatches.Patch(color=DARK_ORANGE, alpha=0.7,
                       label="6kW Fußbodenheizung – EIN-Zonen (orange)"),
        mpatches.Patch(color="#ffbbbb",   alpha=0.9,
                       label="0–30% Notstromreserve (kein EIN)"),
        mlines.Line2D([], [], color="#222222", lw=2.5,
                      label="Betriebsgrenzen 06:00 / 22:00"),
        mlines.Line2D([], [], color="#1155cc", lw=1.5, ls="--",
                      label="Cutover Winter (Nov–Mär): 16:00"),
        mlines.Line2D([], [], color="#7722aa", lw=1.5, ls="--",
                      label="Cutover Frühling/Herbst (Apr+Okt): 17:00"),
        mlines.Line2D([], [], color="#bb4400", lw=1.5, ls="--",
                      label="Cutover Sommer (Mai–Sep): 18:00"),
        mlines.Line2D([], [], color="#2244cc", lw=2.5,
                      label="SOC-Verlauf (Beispiel)"),
        mpatches.Patch(color="#fffde0",   alpha=0.95, ec="#cc8800",
                       label="★ batterie_war_voll → Entlade-Betrieb"),
        mpatches.Patch(color="#ffeedd",   alpha=0.95, ec="#cc6600",
                       label="Entlade-Betrieb Bedingungen"),
        mpatches.Patch(color="#eeeeff",   alpha=0.95, ec="#3355aa",
                       label="Pending-Zeiten (EIN/AUS)"),
    ]
    ax.legend(handles=legend_handles, loc="upper left",
              bbox_to_anchor=(0.0, -0.12),
              fontsize=8, ncol=4, framealpha=0.96,
              edgecolor="#cccccc", title="Legende", title_fontsize=9)

    buf = io.BytesIO()
    plt.savefig(buf, dpi=150, bbox_inches="tight", facecolor="#f8f8f8")
    plt.close()
    buf.seek(0)
    print("✅ Regeldiagramm v6 erstellt.")
    return buf.read()


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

    DARK_GREEN  = "#1a6e1a"
    DARK_ORANGE = "#cc5500"   # 6kW – dunkles Orange (unterscheidet sich von Rot der Notreserve)
    COL_PV      = "#FFA500"
    COL_BAT     = "#4CAF50"
    COL_GRID    = "#5B9BD5"
    COL_CONS    = "#b5a800"

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

    axR.axhline(30,  color="red",        ls="--", lw=1.5, alpha=0.8, zorder=5)
    axR.axhline(75,  color=DARK_GREEN,   ls=":",  lw=1.0, alpha=0.5, zorder=5)
    axR.axhline(80,  color=DARK_ORANGE,  ls=":",  lw=1.0, alpha=0.5, zorder=5)
    axR.axhline(100, color="#aaaaaa",    ls=":",  lw=0.8, alpha=0.4, zorder=5)

    for y, txt, col in [(30, "30% Notreserve",    "red"),
                        (75, "75% (3kW AUS-Schw.)", DARK_GREEN),
                        (80, "80% (6kW AUS-Schw.)", DARK_ORANGE),
                        (100,"100%",                 "#888888")]:
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
        col  = DARK_GREEN if sp["geraet"] == "3kw" else DARK_ORANGE
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
        mpatches.Patch(color=COL_PV,    alpha=0.6, label="PV-Ertrag (W)"),
        mlines.Line2D ([],[],            color=COL_CONS,    lw=2.2,  label="Gesamtverbrauch (W)"),
        mpatches.Patch(color=COL_BAT,   alpha=0.6, label="Batterie (+ entladen / – laden)"),
        mpatches.Patch(color=COL_GRID,  alpha=0.6, label="Netz (+ Bezug / – Einspeisung)"),
        mlines.Line2D ([],[],            color=DARK_GREEN,  lw=2.8,  label="Batterieladezustand (%)"),
        mlines.Line2D ([],[],            color=DARK_GREEN,  marker="o", ms=8, ls="None",
                       markeredgecolor="white", label="3kW Einschalten ●"),
        mlines.Line2D ([],[],            color=DARK_GREEN,  marker="x", ms=9, ls="None",
                       markeredgewidth=2.8,     label="3kW Ausschalten ✕"),
        mlines.Line2D ([],[],            color=DARK_ORANGE, marker="o", ms=8, ls="None",
                       markeredgecolor="white", label="6kW Einschalten ●"),
        mlines.Line2D ([],[],            color=DARK_ORANGE, marker="x", ms=9, ls="None",
                       markeredgewidth=2.8,     label="6kW Ausschalten ✕"),
    ]
    ax1.legend(handles=legend_handles, loc="upper left", fontsize=8.5,
               ncol=2, framealpha=0.92, edgecolor="#cccccc")

    # ── 3kW Heizstab Status ───────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1])
    ax2.set_facecolor("#ffffff")
    ax2.step(x3, y3, where="post", color=DARK_GREEN, lw=2.2)
    ax2.fill_between(x3, 0, y3, step="post", alpha=0.28, color=DARK_GREEN)

    ein3_punkte = [p for p in schaltpunkte if p["geraet"]=="3kw" and p["aktion"]=="EIN"]
    aus3_punkte = [p for p in schaltpunkte if p["geraet"]=="3kw" and p["aktion"]=="AUS"]
    for p in ein3_punkte:
        t      = zeit_zu_min(p["zeit"])
        signal = p.get("modus") or ""
        ax2.scatter([t], [1], color=DARK_GREEN, s=100, zorder=10,
                    marker="o", edgecolors="white", linewidths=1.2)
        ax2.text(t, 1.18, f"EIN  {p['zeit']}\n({signal})" if signal else f"EIN  {p['zeit']}",
                 ha="center", fontsize=8, color=DARK_GREEN, fontweight="bold")
    for p in aus3_punkte:
        t      = zeit_zu_min(p["zeit"])
        signal = p.get("modus") or ""
        ax2.scatter([t], [0], color=DARK_GREEN, s=120, zorder=10,
                    marker="x", linewidths=2.8)
        ax2.text(t, -0.32, f"AUS  {p['zeit']}\n({signal})" if signal else f"AUS  {p['zeit']}",
                 ha="center", fontsize=8, color=DARK_GREEN, fontweight="bold")

    ax2.set_xlim(0, 1440); ax2.set_ylim(-0.65, 1.9)
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
    ax3.step(x6, y6, where="post", color=DARK_ORANGE, lw=2.2)
    ax3.fill_between(x6, 0, y6, step="post", alpha=0.22, color=DARK_ORANGE)

    ein6_punkte = [p for p in schaltpunkte if p["geraet"]=="6kw" and p["aktion"]=="EIN"]
    aus6_punkte = [p for p in schaltpunkte if p["geraet"]=="6kw" and p["aktion"]=="AUS"]
    for p in ein6_punkte:
        t      = zeit_zu_min(p["zeit"])
        signal = p.get("modus") or ""
        ax3.scatter([t], [1], color=DARK_ORANGE, s=100, zorder=10,
                    marker="o", edgecolors="white", linewidths=1.2)
        ax3.text(t, 1.18, f"EIN  {p['zeit']}\n({signal})" if signal else f"EIN  {p['zeit']}",
                 ha="center", fontsize=8, color=DARK_ORANGE, fontweight="bold")
    for p in aus6_punkte:
        t      = zeit_zu_min(p["zeit"])
        signal = p.get("modus") or ""
        ax3.scatter([t], [0], color=DARK_ORANGE, s=120, zorder=10,
                    marker="x", linewidths=2.8)
        ax3.text(t, -0.32, f"AUS  {p['zeit']}\n({signal})" if signal else f"AUS  {p['zeit']}",
                 ha="center", fontsize=8, color=DARK_ORANGE, fontweight="bold")

    ax3.set_xlim(0, 1440); ax3.set_ylim(-0.65, 1.9)
    ax3.set_yticks([0, 1])
    ax3.set_yticklabels(["AUS", "EIN"], fontsize=9, color=DARK_ORANGE)
    ax3.set_xticks(tick_idx); ax3.set_xticklabels(tick_labels, fontsize=9)
    ax3.set_title("③ 6kW Heizstab (Fußbodenheizung) – Schaltzustände",
                  fontsize=11, fontweight="bold", color=DARK_ORANGE)
    ax3.grid(True, alpha=0.2, linestyle="--")
    ax3.axhline(0, color="#aaaaaa", lw=0.8)

    # Schaltpunkte sind direkt an den Markern in ax2 (3kW) und ax3 (6kW) annotiert

    buf = io.BytesIO()
    plt.savefig(buf, dpi=150, bbox_inches="tight", facecolor="#f4f4f4")
    plt.close()
    buf.seek(0)
    print("✅ Tagesdiagramm erstellt.")
    return buf.read()


# ═══════════════════════════════════════════════════════════════════════════════
# LADERATE-ANALYSE DIAGRAMM (Normalbetrieb)
# ═══════════════════════════════════════════════════════════════════════════════
def erstelle_laderate_diagramm(status: dict) -> bytes:
    """
    Erstellt Laderate-Analyse PNG mit zwei Tabellen (3kW / 6kW) + SOC-Kurve.
    Zeitraum: Betriebsstart bis erstes SOC=100% (Normalbetrieb).
    Datenquelle: status['laderate_verlauf']
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
    except ImportError:
        print("❌ matplotlib nicht verfügbar – kein Laderate-Diagramm")
        return None

    DARK_GREEN  = "#1a6e1a"
    DARK_ORANGE = "#cc5500"

    verlauf   = status.get("laderate_verlauf", [])
    datum_str = status.get("tages_datum", lokal_jetzt().strftime("%Y-%m-%d"))

    if not verlauf:
        fig, ax = plt.subplots(figsize=(12, 3), facecolor="#f8f8f8")
        ax.text(0.5, 0.5,
                "Kein Normalbetrieb-Verlauf verfügbar\n"
                "(SOC hat heute nicht 100% erreicht oder keine Daten)",
                ha="center", va="center", transform=ax.transAxes, fontsize=12)
        ax.axis("off")
        buf = io.BytesIO()
        plt.savefig(buf, dpi=120, bbox_inches="tight", facecolor="#f8f8f8")
        plt.close()
        buf.seek(0)
        return buf.read()

    def signal_3kw(soc, ueberschuss, laderate):
        if laderate is None or laderate <= 0:
            return "–"
        if laderate >= 20 and 45 <= soc < 60 and ueberschuss >= 3000:
            return "Schnell"
        if laderate >= 15 and 60 <= soc < 75 and ueberschuss >= 3000:
            return "Mittel"
        if laderate > 0  and soc >= 75 and ueberschuss >= 3000:
            return "Langsam"
        return "–"

    def signal_6kw(soc, ueberschuss, laderate):
        if laderate is None or laderate <= 0:
            return "–"
        if laderate >= 20 and soc >= 75 and ueberschuss >= 6300:
            return "Schnell"
        if laderate >= 15 and soc >= 83 and ueberschuss >= 5000:
            return "Mittel"
        if laderate > 0  and soc >= 90 and ueberschuss >= 4000:
            return "Langsam"
        return "–"

    headers   = ["Zeit", "SOC %", "Übersch. W", "Laderate %/h", "# Pkte", "Signal"]
    rows_3kw  = []
    rows_6kw  = []
    zeiten    = []
    soc_werte = []

    for e in verlauf:
        lr  = e.get("laderate")
        lr_str = f"{lr:.1f}" if lr is not None else "–"
        soc = e["soc"]
        ueb = e["ueberschuss"]
        n   = e["n_punkte"]
        zeit = e["zeit"]
        sig3 = signal_3kw(soc, ueb, lr)
        sig6 = signal_6kw(soc, ueb, lr)
        rows_3kw.append([zeit, str(soc), str(ueb), lr_str, str(n), sig3])
        rows_6kw.append([zeit, str(soc), str(ueb), lr_str, str(n), sig6])
        zeiten.append(zeit)
        soc_werte.append(soc)

    n_rows = len(rows_3kw)

    # Figurhöhe dynamisch: Tabelle braucht ~0.22 inch pro Zeile, min 3 inch
    tbl_h   = max(3.0, n_rows * 0.22 + 1.2)
    soc_h   = 3.5
    fig_h   = min(tbl_h * 2 + soc_h + 1.5, 80)   # max 80 inch

    fig = plt.figure(figsize=(14, fig_h), facecolor="#f8f8f8")
    gs  = gridspec.GridSpec(3, 1,
                            height_ratios=[tbl_h, tbl_h, soc_h],
                            hspace=0.5,
                            left=0.02, right=0.98,
                            top=0.98, bottom=0.01)

    def zeichne_tabelle(ax, titel, rows, farbe):
        ax.axis("off")
        ax.set_title(titel, fontsize=11, fontweight="bold", color=farbe, pad=8)
        if not rows:
            ax.text(0.5, 0.5, "Keine Daten", ha="center", va="center",
                    transform=ax.transAxes, fontsize=10)
            return
        tbl = ax.table(
            cellText=rows,
            colLabels=headers,
            loc="center",
            cellLoc="center",
        )
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(8)
        tbl.auto_set_column_width(col=list(range(len(headers))))

        # Kopfzeile
        for j in range(len(headers)):
            tbl[0, j].set_facecolor(farbe)
            tbl[0, j].set_text_props(color="white", fontweight="bold")

        # Zeilen: Signal-Zeilen hervorheben, sonst abwechselnd
        sig_bg_ja  = "#c8eec8" if farbe == DARK_GREEN else "#ffe0b0"
        for i, row in enumerate(rows, 1):
            signal = row[-1]
            if signal != "–":
                bg = sig_bg_ja
            elif i % 2 == 0:
                bg = "#f0f0f0"
            else:
                bg = "#ffffff"
            for j in range(len(headers)):
                tbl[i, j].set_facecolor(bg)

    ax1 = fig.add_subplot(gs[0])
    zeichne_tabelle(ax1,
                    f"① 3kW Brauchwasser – Normalbetrieb {datum_str}  "
                    f"(Schwellen: Schnell SOC 45–59% ≥20%/h ≥3000W | "
                    f"Mittel SOC 60–74% ≥15%/h ≥3000W | Langsam SOC ≥75% >0 ≥3000W)",
                    rows_3kw, DARK_GREEN)

    ax2 = fig.add_subplot(gs[1])
    zeichne_tabelle(ax2,
                    f"② 6kW Fußbodenheizung – Normalbetrieb {datum_str}  "
                    f"(Schwellen: Schnell SOC ≥75% ≥20%/h ≥6300W | "
                    f"Mittel SOC ≥83% ≥15%/h ≥5000W | Langsam SOC ≥90% >0 ≥4000W)",
                    rows_6kw, DARK_ORANGE)

    # ── SOC-Kurve ─────────────────────────────────────────────────────────────
    ax3 = fig.add_subplot(gs[2])
    ax3.set_facecolor("#ffffff")

    x = list(range(n_rows))
    ax3.plot(x, soc_werte, color=DARK_GREEN, lw=2.5, zorder=5)
    ax3.fill_between(x, 0, soc_werte, alpha=0.12, color=DARK_GREEN)

    # Schwellenlinien mit Beschriftung
    schwellen = [
        (45, "#2e7d32", "45% Schnell 3kW"),
        (60, "#43a047", "60% Mittel 3kW"),
        (75, DARK_GREEN, "75% Langsam 3kW / Schnell 6kW"),
        (83, DARK_ORANGE, "83% Mittel 6kW"),
        (90, "#bf360c", "90% Langsam 6kW"),
    ]
    for y_val, col, lbl in schwellen:
        ax3.axhline(y_val, color=col, lw=1.0, ls="--", alpha=0.7, zorder=3)
        ax3.text(n_rows * 1.005, y_val, lbl, fontsize=7, color=col,
                 va="center", ha="left", clip_on=False)

    # X-Achse: max. 12 Beschriftungen
    if n_rows > 0:
        tick_step = max(1, n_rows // 12)
        tick_idx  = list(range(0, n_rows, tick_step))
        ax3.set_xticks(tick_idx)
        ax3.set_xticklabels([zeiten[i] for i in tick_idx], fontsize=8, rotation=45)

    ax3.set_xlim(0, max(1, n_rows - 1))
    ax3.set_ylim(0, 105)
    ax3.set_ylabel("SOC (%)", fontsize=10)
    ax3.set_title("③ SOC-Verlauf Normalbetrieb (06:00 → erstes SOC=100%)",
                  fontsize=11, fontweight="bold")
    ax3.grid(True, alpha=0.2, linestyle="--")

    buf = io.BytesIO()
    plt.savefig(buf, dpi=120, bbox_inches="tight", facecolor="#f8f8f8")
    plt.close()
    buf.seek(0)
    print("✅ Laderate-Analyse erstellt.")
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
        status_zeile = (f"✅  AUTOMATION LÄUFT NORMAL\n"
                        f"   Betriebszeit: {BETRIEB_START_STUNDE:02d}:00 – "
                        f"{BETRIEB_ENDE_STUNDE:02d}:00 Uhr (aktiv)")

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
        (letzter_morgen is None or letzter_morgen != heute_str)
        and now_utc_h == MORGENREPORT_UTC_STUNDE
        and now_utc_m < REPORT_FENSTER_MIN
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
        (letzter_abend is None or letzter_abend != heute_str)
        and now_utc_h == ABENDREPORT_UTC_STUNDE
        and now_utc_m < REPORT_FENSTER_MIN
    )
    if abend_faellig:
        print("📊 Abend-Report wird erstellt...")
        # Offizielle iSolarCloud-Werte bevorzugen, Fallback auf 5-Min-Schätzung
        energie = status.get("tages_energie_isolarcloud") or berechne_tages_energie(
            status.get("tages_verlauf", []))
        quelle = "iSolarCloud" if status.get("tages_energie_isolarcloud") else "Schätzung"
        print(f"ℹ️  Energiedaten-Quelle: {quelle}")
        png_bytes  = erstelle_tagesdiagramm(status)
        png_bytes2 = erstelle_regeldiagramm()
        png_bytes3 = erstelle_laderate_diagramm(status)
        betreff, text = erstelle_abendreport_text(status, energie)
        sende_email_mit_anhang(betreff, text, png_bytes, png_bytes2, png_bytes3)
        status["abendreport_datum"] = heute_str
        print("✅ Abend-Report gesendet.")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main() -> None:
    print("=== PV MONITOR START ===")
    lokal = lokal_jetzt()
    print(f"Zeit: {lokal.strftime('%d.%m.%Y %H:%M:%S')} (CEST)")

    status = lade_status()

    # Neuer-Tag-Reset (Tagesdaten, Schaltpunkte)
    reset_wenn_neuer_tag(status)

    # ── Betriebszeit prüfen (06:00 – 22:00 Uhr CEST) ────────────────────────
    if not ist_betriebszeit():
        heute_str = lokal.strftime("%Y-%m-%d")
        if ist_abschaltzeitfenster() and status.get("abschalt_pruefung_datum") != heute_str:
            # Einmalig pro Tag: Abschalt-Prüfung beim ersten Lauf nach 22:00 Uhr
            print("🌙 Betriebsende 22:00 Uhr – Abschalt-Prüfung wird durchgeführt")
            status["abschalt_pruefung_datum"] = heute_str
            tydom_email    = os.environ.get("TYDOM_EMAIL")
            tydom_passwort = os.environ.get("TYDOM_PASSWORD")
            if tydom_email and tydom_passwort:
                fuehre_abschalt_pruefung_durch(tydom_email, tydom_passwort, status)
            else:
                print("❌ TYDOM Zugangsdaten fehlen – Abschalt-Prüfung nicht möglich")
            speichere_status(status)
        else:
            print(f"⏸️  Außerhalb Betriebszeit "
                  f"({lokal.hour:02d}:{lokal.minute:02d} Uhr CEST, "
                  f"aktiv: {BETRIEB_START_STUNDE:02d}:00–{BETRIEB_ENDE_STUNDE:02d}:00) – Exit")
        print("=== PV MONITOR ENDE (außerhalb Betriebszeit) ===")
        return

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

    # Laderate-Verlauf: nur im Normalbetrieb (solange SOC heute noch nicht 100% erreicht hat)
    if not status.get("batterie_war_voll", False):
        _lr  = berechne_laderate(status["soc_verlauf"])
        _ev  = status.get("laderate_verlauf", [])
        _ev.append({
            "zeit":        lokal_jetzt().strftime("%H:%M"),
            "soc":         daten["batterie_prozent"],
            "ueberschuss": daten["ueberschuss_w"],
            "laderate":    round(_lr, 1) if _lr is not None else None,
            "n_punkte":    len(status["soc_verlauf"]),
        })
        status["laderate_verlauf"] = _ev

    # Offizielle iSolarCloud Tagesdaten speichern (für Abend-Report)
    if daten.get("tages_energie"):
        status["tages_energie_isolarcloud"] = daten["tages_energie"]

    # Tagesberichte prüfen (vor Pause-Check, damit Reports auch bei Pause kommen)
    verarbeite_tagesberichte(status)

    # Pausen prüfen
    if ist_automation_pausiert():
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
