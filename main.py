"""
PV-Heizstab-Automation – Hauptscript v2.0
Vollstaendige TYDOM-Steuerung mit Schaltlogik gemaess Konzept-Dokument v1.2
"""
import asyncio
import hashlib
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
from email.mime.text import MIMEText

import requests
import websockets

# ═══════════════════════════════════════════════════════════════════════════════
# KONSTANTEN
# ═══════════════════════════════════════════════════════════════════════════════
STATUS_DATEI            = "status.json"
AUTOMATION_PAUSE_DATEI  = "automation_pause.json"
ISOLARCLOUD_API_URL     = "https://gateway.isolarcloud.eu"

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

# iSolarCloud Messpunkte
MP_SOC          = "13141"
MP_HAUS         = "13119"
MP_NETZ_IMPORT  = "13149"
MP_EINSPEISUNG  = "13121"
MP_BAT_LADEN    = "13126"
MP_BAT_ENTLADEN = "13150"

# Saison 6kW: erlaubt 01.Oktober bis 15.Mai
SAISON_6KW_START = (10, 1)
SAISON_6KW_ENDE  = (5, 15)

# Pending-Mindestwartezeit in Sekunden (5-Min-Zyklus -> 3 Min reichen als Puffer)
PENDING_SEKUNDEN = 180


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

def benachrichtige(titel: str, text: str, prioritaet: int = 0) -> None:
    zeit  = datetime.now().strftime("%d.%m.%Y %H:%M")
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
                                    MP_EINSPEISUNG, MP_BAT_LADEN, MP_BAT_ENTLADEN]},
            headers=_isc_headers(secret_key), timeout=15)
        d = r.json()
        if d.get("result_code") == "1":
            ep = d["result_data"]["device_point_list"][0]["device_point"]
            def w(key):
                return int(round(float(ep.get(f"p{key}") or 0)))
            soc          = int(round(float(ep.get(f"p{MP_SOC}") or 0) * 100))
            haus         = w(MP_HAUS)
            netz_import  = w(MP_NETZ_IMPORT)
            einspeisung  = w(MP_EINSPEISUNG)
            bat_laden    = w(MP_BAT_LADEN)
            bat_entladen = w(MP_BAT_ENTLADEN)
            pv           = max(0, haus + einspeisung + bat_laden - netz_import - bat_entladen)
            ueberschuss  = max(0, einspeisung + bat_laden - netz_import - bat_entladen)
            print(f"✅ iSolarCloud: SOC={soc}% PV={pv}W Netz={netz_import}W "
                  f"Einsp.={einspeisung}W Uebers.={ueberschuss}W")
            return {"batterie_prozent": soc, "pv_leistung_w": pv,
                    "hausverbrauch_w": haus, "netzbezug_w": netz_import,
                    "einspeisung_w": einspeisung, "ueberschuss_w": ueberschuss}
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
        ssl=ssl_ctx, open_timeout=20, ping_interval=None,
    ) as ws:
        # Init + Geraetedaten anfordern
        for methode, pfad in [("GET", "/ping"), ("POST", "/refresh/all"),
                               ("GET", "/devices/data")]:
            await ws.send(_http_msg(methode, pfad))
            await asyncio.sleep(0.3)

        # Szenarien ausfuehren
        if szenarien_ids:
            for scn_id in szenarien_ids:
                await ws.send(_http_msg("POST", f"/scenarios/{scn_id}/leftover", "{}"))
                print(f"   ▶️  Szenario {scn_id} gesendet")
                await asyncio.sleep(0.5)
            # Nach dem Schalten: Zustand nochmal lesen
            # 10s warten – physisches Relais braucht Zeit zum Schalten und Melden
            await asyncio.sleep(10)
            await ws.send(_http_msg("GET", "/devices/data"))
            await asyncio.sleep(0.3)

        # Antworten sammeln
        device_data = None
        start = time.time()
        while time.time() - start < 15:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
                _, body = _parse_nachricht(raw)
                if isinstance(body, list) and any("endpoints" in str(g) for g in body):
                    device_data = body
            except asyncio.TimeoutError:
                if time.time() - start > 8:
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
    verlauf.append({"soc": soc, "zeit": datetime.now().isoformat()})
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
# SAISON UND PAUSEN
# ═══════════════════════════════════════════════════════════════════════════════
def ist_6kw_saison() -> bool:
    m, d = datetime.now().month, datetime.now().day
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
        if datetime.now().date() <= pause_bis:
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
        if datetime.now() < bis:
            print(f"⏸️  2h-Sperre aktiv bis {bis.strftime('%H:%M')}")
            return True
        status["manuell_sperre_bis"] = None
    except Exception:
        status["manuell_sperre_bis"] = None
    return False

def ist_pending_bestaetigt(pending_seit: str) -> bool:
    if not pending_seit:
        return False
    try:
        return (datetime.now() - datetime.fromisoformat(pending_seit)).total_seconds() >= PENDING_SEKUNDEN
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

    # Einspeisung-Stopp (hoechste Prioritaet, kein Delay)
    if einspeisung > 0 and soc >= 99 and pv >= 1000:
        return True, "EINSPEISUNG_STOPP", True

    # Nach SOC-Abschaltung: erst wieder wenn SOC >= 85 und Batterie laedt
    if status.get("soc_abschaltung_3kw"):
        if soc >= 85 and laderate is not None and laderate > 0:
            return True, "NORMAL", False
        return False, None, False

    # Hochspeicher
    if soc >= 97 and pv >= 2000:
        return True, "HOCHSPEICHER", False

    # Normal (Batterie muss laden)
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
    """Returns True wenn 3kW ausgeschaltet werden soll."""
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
        # Zurueck in Normal-Modus – nicht ausschalten, nur Modus aendern
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

    # Nach SOC-Abschaltung: erst wenn SOC = 100%
    if status.get("soc_abschaltung_6kw"):
        if soc >= 99 and laderate is not None and laderate >= 0:
            return True, "NORMAL", False
        return False, None, False

    # Hochspeicher
    if soc >= 99 and pv >= 4500:
        return True, "HOCHSPEICHER", False

    # Normal (Batterie muss laden)
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
    """Returns True wenn 6kW ausgeschaltet werden soll."""
    soc         = daten["batterie_prozent"]
    ueberschuss = daten["ueberschuss_w"]
    netzbezug   = daten["netzbezug_w"]
    pv          = daten["pv_leistung_w"]
    modus       = status.get("modus_6kw", "NORMAL")

    if netzbezug > 300:
        return True
    if ueberschuss < 4000:   # Deckt auch die < 2000W Regel ab
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
    now_str     = datetime.now().isoformat()
    szenarien   = []
    meldungen   = []
    soc         = daten["batterie_prozent"]
    ueberschuss = daten["ueberschuss_w"]
    netzbezug   = daten["netzbezug_w"]

    # Tagesstatistik
    heute_str   = datetime.now().strftime("%Y-%m-%d")
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

    # Prüfen ob kurz vorher ein Schaltbefehl gesendet wurde (max. 15 Min.)
    # Falls ja: kein 2h-Lock – Gerät könnte noch schalten
    letzte_schalt = status.get("letzte_schaltzeit")
    schalt_kuerzlich = False
    if letzte_schalt:
        try:
            diff = (datetime.now() - datetime.fromisoformat(letzte_schalt)).total_seconds()
            schalt_kuerzlich = diff < 900  # 15 Minuten
        except Exception:
            pass

    if tydom_3kw != ist_3kw_ein:
        if not tydom_3kw and ist_3kw_ein:
            if schalt_kuerzlich:
                # Gerät schaltet noch – kein Lock, Zustand aus TYDOM übernehmen
                print("ℹ️  3kW AUS nach Schaltbefehl – Gerät schaltet noch, kein 2h-Lock")
            else:
                # Manuell AUS -> 2h-Sperre
                bis = (datetime.now() + timedelta(hours=2)).strftime("%H:%M")
                status["manuell_sperre_bis"] = (datetime.now() + timedelta(hours=2)).isoformat()
                status["modus_3kw"] = None
                msg = f"🖐️ 3kW manuell ausgeschaltet – Automatik gesperrt bis {bis}"
                print(msg);  meldungen.append(msg)
        else:
            print("ℹ️  3kW manuell EIN erkannt – Automatik laeuft weiter.")
        status["heizstab_3kw_ein"] = tydom_3kw

    if tydom_6kw != ist_6kw_ein:
        if not tydom_6kw and ist_6kw_ein:
            if schalt_kuerzlich:
                print("ℹ️  6kW AUS nach Schaltbefehl – Gerät schaltet noch, kein 2h-Lock")
            else:
                bis = (datetime.now() + timedelta(hours=2)).strftime("%H:%M")
                status["manuell_sperre_bis"] = (datetime.now() + timedelta(hours=2)).isoformat()
                status["modus_6kw"] = None
                msg = f"🖐️ 6kW manuell ausgeschaltet – Automatik gesperrt bis {bis}"
                print(msg);  meldungen.append(msg)
        else:
            print("ℹ️  6kW manuell EIN erkannt – Automatik laeuft weiter.")
        status["heizstab_6kw_ein"] = tydom_6kw

    # Aktuellen Zustand aus Status lesen (nach Abgleich)
    ein_3kw = status.get("heizstab_3kw_ein", False)
    ein_6kw = status.get("heizstab_6kw_ein", False)

    # Ladegeschwindigkeit
    laderate     = berechne_laderate(status.get("soc_verlauf", []))
    laderate_str = f"{laderate:.1f}%/h" if laderate is not None else "unbekannt"
    print(f"ℹ️  SOC={soc}% | Laderate={laderate_str} | Uebers.={ueberschuss}W | Netz={netzbezug}W")
    if not ist_6kw_saison():
        print("ℹ️  6kW: Sommersperre aktiv (16.Mai-30.Sep)")

    # ── 6kW AUSSCHALTEN (Prioritaet: zuerst abschalten) ─────────────────────
    if ein_6kw:
        soll_aus = pruefe_6kw_ausschalten(daten, status)
        if soll_aus:
            if not status.get("ausschalt_pending_6kw"):
                status["ausschalt_pending_6kw"] = now_str
                print("⏳ 6kW AUS: Pending (Bestaetigung naechster Zyklus)")
            elif ist_pending_bestaetigt(status.get("ausschalt_pending_6kw")):
                print("🔴 6kW wird AUSGESCHALTET")
                szenarien.append(SCN_AUS_6KW)
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
                print("ℹ️  6kW AUS Pending geloescht (Bedingung entfallen)")

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
                status["heizstab_3kw_ein"]      = True
                status["modus_3kw"]             = modus
                status["einschalt_pending_3kw"] = None
                status["soc_abschaltung_3kw"]   = False
                schaltungen["ein_3kw"] += 1
                meldungen.append(
                    f"🟢 3kW EIN (Einspeisung-Stopp)\n"
                    f"SOC: {soc}% | PV: {daten['pv_leistung_w']}W | "
                    f"Einspeisung: {daten.get('einspeisung_w',0)}W")
            else:
                if not status.get("einschalt_pending_3kw"):
                    status["einschalt_pending_3kw"] = now_str
                    print(f"⏳ 3kW EIN ({modus}): Pending")
                elif ist_pending_bestaetigt(status.get("einschalt_pending_3kw")):
                    print(f"🟢 3kW wird EINGESCHALTET ({modus})")
                    szenarien.append(SCN_EIN_3KW)
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
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def main() -> None:
    print("=== PV MONITOR START ===")
    print(f"Zeit: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")

    status = lade_status()

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

    # SOC-Verlauf aktualisieren (immer, auch bei Pause)
    status["soc_verlauf"] = aktualisiere_soc_verlauf(status, daten["batterie_prozent"])

    # Pausen pruefen
    if ist_automation_pausiert() or ist_manuell_pausiert(status):
        status["letzte_aktualisierung"] = datetime.now().isoformat()
        speichere_status(status)
        print("=== PV MONITOR ENDE (Pause) ===")
        return

    # TYDOM Zugangsdaten pruefen
    tydom_email   = os.environ.get("TYDOM_EMAIL")
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
        status["letzte_aktualisierung"] = datetime.now().isoformat()
        speichere_status(status)
        print("=== PV MONITOR ENDE ===")
        return

    # Schaltlogik anwenden
    print("--- Schaltlogik ---")
    szenarien, meldungen = verarbeite_schaltlogik(daten, status, tydom_zustand)

    # Szenarien ausfuehren (falls noetig)
    if szenarien:
        print(f"--- TYDOM Schalten ({len(szenarien)} Szenario(en)) ---")
        tydom_ergebnis = tydom_ausfuehren(tydom_email, tydom_passwort, szenarien)
        # Schaltzeit merken (verhindert falschen 2h-Lock im nächsten Zyklus)
        status["letzte_schaltzeit"] = datetime.now().isoformat()

        if tydom_ergebnis:
            # Bestätigung nur loggen – Status NICHT überschreiben.
            # Das Relais braucht manchmal länger als die Wartezeit.
            # Der nächste Zyklus verifiziert den echten Zustand über den manuellen-
            # Eingriff-Vergleich (ohne 2h-Lock, weil letzte_schaltzeit gesetzt ist).
            print(f"ℹ️  TYDOM nach Schalten: "
                  f"3kW={'EIN' if tydom_ergebnis.get('3kw_ein') else 'AUS'}, "
                  f"6kW={'EIN' if tydom_ergebnis.get('6kw_ein') else 'AUS'} "
                  f"– Zustand wird nächsten Zyklus verifiziert")
        else:
            print("⚠️  TYDOM Bestätigungslesung fehlgeschlagen – Status bleibt wie gesetzt.")

        # Benachrichtigungen senden
        for meldung in meldungen:
            if "🖐️" not in meldung:  # Manuelle Eingriffe bereits gemeldet
                benachrichtige("PV Heizstab", meldung)
    else:
        print("ℹ️  Kein Schalten erforderlich.")

    # Manuelle Eingriff-Meldungen senden
    for meldung in meldungen:
        if "🖐️" in meldung:
            benachrichtige("PV Heizstab – Manuelle Aktion", meldung, prioritaet=1)

    status["letzte_aktualisierung"] = datetime.now().isoformat()
    speichere_status(status)
    print("=== PV MONITOR ENDE ===")


if __name__ == "__main__":
    main()
