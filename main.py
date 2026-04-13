import json
import os
from datetime import datetime

import requests

STATUS_DATEI = "status.json"
ISOLARCLOUD_API_URL = "https://gateway.isolarcloud.eu"

# Messpunkt-IDs für Energy Storage System (V1, Hybrid-Wechselrichter mit Batterie)
MESSPUNKT_BATTERIE_SOC = "13141"    # Batterie-Ladestand (SOC) in %
MESSPUNKT_HAUSVERBRAUCH = "13119"   # Hausverbrauch (Load Power) in W
MESSPUNKT_NETZBEZUG = "13149"       # Netzbezug (Purchased Power) in W
MESSPUNKT_EINSPEISUNG = "13121"     # Einspeisung (Feed-in Power) in W
MESSPUNKT_BAT_LADEN = "13126"       # Batterie Ladeleistung in W
MESSPUNKT_BAT_ENTLADEN = "13150"    # Batterie Entladeleistung in W


def lade_status() -> dict:
    """
    Lädt den zuletzt gespeicherten Status aus der JSON-Datei.
    """
    if not os.path.exists(STATUS_DATEI):
        return {"letzter_status": ""}

    try:
        with open(STATUS_DATEI, "r", encoding="utf-8") as datei:
            return json.load(datei)
    except Exception as fehler:
        print("❌ Fehler beim Laden von status.json:", str(fehler))
        return {"letzter_status": ""}


def speichere_status(status: dict) -> None:
    """
    Speichert den aktuellen Status in die JSON-Datei.
    """
    try:
        with open(STATUS_DATEI, "w", encoding="utf-8") as datei:
            json.dump(status, datei, ensure_ascii=False, indent=2)
        print("✅ status.json wurde gespeichert.")
    except Exception as fehler:
        print("❌ Fehler beim Speichern von status.json:", str(fehler))


def sende_pushover(titel: str, nachricht: str, prioritaet: int = 0) -> None:
    """
    Sendet eine Push-Nachricht über Pushover.

    Priorität:
        -2 = lautlos
        -1 = unauffällig
         0 = normal
         1 = wichtig
    """
    user_key = os.environ.get("PUSHOVER_USER_KEY")
    api_token = os.environ.get("PUSHOVER_API_TOKEN")

    if not user_key or not api_token:
        print("❌ Pushover-Zugangsdaten fehlen!")
        print("USER_KEY vorhanden:", bool(user_key))
        print("API_TOKEN vorhanden:", bool(api_token))
        return

    daten = {
        "token": api_token,
        "user": user_key,
        "title": titel,
        "message": nachricht,
        "priority": prioritaet,
    }

    try:
        antwort = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=daten,
            timeout=10,
        )
        print("Pushover Statuscode:", antwort.status_code)
        print("Pushover Antwort:", antwort.text)
    except Exception as fehler:
        print("❌ Fehler bei Pushover:", str(fehler))


def sende_email(betreff: str, inhalt: str) -> None:
    """
    Platzhalter für späteren E-Mail-Versand.
    """
    print("📧 E-MAIL TEST")
    print(f"BETREFF: {betreff}")
    print("INHALT:")
    print(inhalt)
    print("-" * 60)


def hole_testdaten() -> dict:
    """
    Testdaten für die Entwicklung.
    Diese Funktion wird nicht mehr aufgerufen, bleibt aber erhalten.
    """
    return {
        "batterie_prozent": 92,
        "pv_leistung_w": 5200,
        "hausverbrauch_w": 1400,
        "netzbezug_w": 0,
        "ueberschuss_w": 3800,
    }


def isolarcloud_login(app_key: str, secret_key: str, user_account: str, user_password: str):
    """
    Meldet sich bei iSolarCloud an und gibt den Token zurück.
    Gibt None zurück, wenn der Login fehlschlägt.
    """
    url = f"{ISOLARCLOUD_API_URL}/openapi/login"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "x-access-key": secret_key,
        "sys_code": "901",
    }
    body = {
        "appkey": app_key,
        "user_account": user_account,
        "user_password": user_password,
    }
    try:
        antwort = requests.post(url, json=body, headers=headers, timeout=15)
        daten = antwort.json()
        if daten.get("result_code") == "1":
            token = daten["result_data"]["token"]
            print("✅ iSolarCloud Login erfolgreich.")
            return token
        else:
            print("❌ iSolarCloud Login fehlgeschlagen:", daten.get("result_msg"))
            print("Antwort:", antwort.text)
            return None
    except Exception as fehler:
        print("❌ Fehler beim iSolarCloud Login:", str(fehler))
        return None


def isolarcloud_get_ps_id(app_key: str, secret_key: str, token: str):
    """
    Ruft die Anlagen-ID (ps_id) der ersten gefundenen Anlage ab.
    Gibt None zurück, wenn keine Anlage gefunden wird.
    """
    url = f"{ISOLARCLOUD_API_URL}/openapi/getPowerStationList"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "x-access-key": secret_key,
        "sys_code": "901",
    }
    body = {
        "appkey": app_key,
        "token": token,
        "curPage": 1,
        "size": 10,
    }
    try:
        antwort = requests.post(url, json=body, headers=headers, timeout=15)
        daten = antwort.json()
        if daten.get("result_code") == "1":
            anlagen = daten["result_data"]["pageList"]
            if anlagen:
                ps_id = str(anlagen[0]["ps_id"])
                ps_name = anlagen[0].get("ps_name", "?")
                print(f"✅ Anlage gefunden: ps_id={ps_id}, Name={ps_name}")
                return ps_id
            else:
                print("❌ Keine Anlage im Konto gefunden.")
                return None
        else:
            print("❌ Anlagenabfrage fehlgeschlagen:", daten.get("result_msg"))
            print("Antwort:", antwort.text)
            return None
    except Exception as fehler:
        print("❌ Fehler bei Anlagenabfrage:", str(fehler))
        return None


def isolarcloud_get_device_info(app_key: str, secret_key: str, token: str, ps_id: str):
    """
    Ruft die Geräteliste der Anlage ab und gibt ps_key und device_type
    des Hybrid-Wechselrichters (Energy Storage System) zurück.
    Gibt (None, None) bei Fehler zurück.
    """
    url = f"{ISOLARCLOUD_API_URL}/openapi/getDeviceList"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "x-access-key": secret_key,
        "sys_code": "901",
    }
    body = {
        "appkey": app_key,
        "token": token,
        "ps_id": ps_id,
        "curPage": 1,
        "size": 50,
        "is_virtual_unit": "0",
    }
    try:
        antwort = requests.post(url, json=body, headers=headers, timeout=15)
        daten = antwort.json()
        if daten.get("result_code") == "1":
            geraete = daten["result_data"]["pageList"]
            print(f"✅ Geräteliste erhalten: {len(geraete)} Gerät(e)")
            for g in geraete:
                print(
                    f"   → Typ {g.get('device_type'):>3}: "
                    f"{g.get('device_name')} | "
                    f"ps_key={g.get('ps_key')} | "
                    f"Modell={g.get('device_model_code')}"
                )

            # ESS / Hybrid-Wechselrichter bevorzugt (Typ 14)
            for g in geraete:
                if g.get("device_type") == 14:
                    print(f"✅ ESS-Gerät gefunden (Typ 14): {g.get('device_name')}")
                    return g.get("ps_key"), 14

            # Fallback: Typ 22
            for g in geraete:
                if g.get("device_type") == 22:
                    print(f"✅ ESS-Gerät gefunden (Typ 22): {g.get('device_name')}")
                    return g.get("ps_key"), 22

            # Fallback: erstes Gerät das kein reiner Wechselrichter (1) oder
            # Kommunikationsgerät (64, 3, 11, 17) ist
            for g in geraete:
                typ = g.get("device_type")
                if typ not in [1, 3, 11, 17, 64]:
                    print(f"⚠️ Kein Typ 14/22 gefunden, verwende Typ {typ}: {g.get('device_name')}")
                    return g.get("ps_key"), typ

            # Letzter Fallback: erstes verfügbares Gerät
            if geraete:
                g = geraete[0]
                print(f"⚠️ Fallback auf erstes Gerät Typ {g.get('device_type')}: {g.get('device_name')}")
                return g.get("ps_key"), g.get("device_type")

            print("❌ Keine Geräte in der Anlage gefunden.")
            return None, None
        else:
            print("❌ Geräteabfrage fehlgeschlagen:", daten.get("result_msg"))
            print("Antwort:", antwort.text)
            return None, None
    except Exception as fehler:
        print("❌ Fehler bei Geräteabfrage:", str(fehler))
        return None, None


def hole_isolarcloud_daten(app_key: str, secret_key: str, user_account: str, user_password: str):
    """
    Holt Echtdaten von iSolarCloud über V1 Gerätendpunkt.
    Gibt ein Dict mit Messwerten zurück, oder None bei Fehler.
    """
    token = isolarcloud_login(app_key, secret_key, user_account, user_password)
    if not token:
        return None

    ps_id = isolarcloud_get_ps_id(app_key, secret_key, token)
    if not ps_id:
        return None

    ps_key, device_type = isolarcloud_get_device_info(app_key, secret_key, token, ps_id)
    if not ps_key:
        return None

    url = f"{ISOLARCLOUD_API_URL}/openapi/getDeviceRealTimeData"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "x-access-key": secret_key,
        "sys_code": "901",
    }
    body = {
        "appkey": app_key,
        "token": token,
        "ps_key_list": [ps_key],
        "device_type": device_type,
        "point_id_list": [
            MESSPUNKT_BATTERIE_SOC,
            MESSPUNKT_HAUSVERBRAUCH,
            MESSPUNKT_NETZBEZUG,
            MESSPUNKT_EINSPEISUNG,
            MESSPUNKT_BAT_LADEN,
            MESSPUNKT_BAT_ENTLADEN,
        ],
    }
    try:
        antwort = requests.post(url, json=body, headers=headers, timeout=15)
        daten = antwort.json()
        print("iSolarCloud Antwort:", antwort.text[:500])

        if daten.get("result_code") == "1":
            eintrag = daten["result_data"]["device_point_list"][0]["device_point"]

            batterie = int(round(float(eintrag.get(f"p{MESSPUNKT_BATTERIE_SOC}") or 0) * 100))
            haus = int(round(float(eintrag.get(f"p{MESSPUNKT_HAUSVERBRAUCH}") or 0)))
            netz_import = int(round(float(eintrag.get(f"p{MESSPUNKT_NETZBEZUG}") or 0)))
            feed_in = int(round(float(eintrag.get(f"p{MESSPUNKT_EINSPEISUNG}") or 0)))
            bat_laden = int(round(float(eintrag.get(f"p{MESSPUNKT_BAT_LADEN}") or 0)))
            bat_entladen = int(round(float(eintrag.get(f"p{MESSPUNKT_BAT_ENTLADEN}") or 0)))

            pv = max(0, haus + feed_in + bat_laden - netz_import - bat_entladen)
            ueberschuss = max(0, feed_in + bat_laden - netz_import - bat_entladen)

            print("✅ iSolarCloud Echtdaten empfangen:")
            print(f"   Batterie: {batterie}%")
            print(f"   PV-Leistung (berechnet): {pv} W")
            print(f"   Hausverbrauch: {haus} W")
            print(f"   Netzbezug: {netz_import} W")
            print(f"   Einspeisung: {feed_in} W")
            print(f"   Batterie laden: {bat_laden} W")
            print(f"   Batterie entladen: {bat_entladen} W")
            print(f"   Überschuss (berechnet): {ueberschuss} W")

            return {
                "batterie_prozent": batterie,
                "pv_leistung_w": pv,
                "hausverbrauch_w": haus,
                "netzbezug_w": netz_import,
                "ueberschuss_w": ueberschuss,
            }
        else:
            print("❌ Echtdaten-Abfrage fehlgeschlagen:", daten.get("result_msg"))
            print("Antwort:", antwort.text)
            return None
    except Exception as fehler:
        print("❌ Fehler bei Echtdaten-Abfrage:", str(fehler))
        return None


def bewerte_status(daten: dict) -> dict:
    """
    Bewertet die aktuelle Situation und gibt einen Status zurück.
    """
    batterie = daten["batterie_prozent"]
    pv = daten["pv_leistung_w"]
    haus = daten["hausverbrauch_w"]
    netz = daten["netzbezug_w"]
    ueberschuss = daten["ueberschuss_w"]

    if netz > 200:
        return {
            "status": "NETZBEZUG_WARNUNG",
            "titel": "⚠️ PV Warnung",
            "prioritaet": 1,
            "nachricht": (
                f"Netzbezug erkannt!\n\n"
                f"Batterie: {batterie}%\n"
                f"PV-Leistung: {pv} W\n"
                f"Hausverbrauch: {haus} W\n"
                f"Überschuss: {ueberschuss} W\n"
                f"Netzbezug: {netz} W\n\n"
                f"👉 Empfehlung: Heizstab ausschalten"
            ),
        }

    if batterie >= 94 and ueberschuss >= 6300:
        return {
            "status": "HEIZSTAB_6KW",
            "titel": "🔥 PV Hinweis",
            "prioritaet": 0,
            "nachricht": (
                f"6 kW Heizstab sinnvoll\n\n"
                f"Batterie: {batterie}%\n"
                f"PV-Leistung: {pv} W\n"
                f"Hausverbrauch: {haus} W\n"
                f"Überschuss: {ueberschuss} W\n"
                f"Netzbezug: {netz} W"
            ),
        }

    if batterie >= 88 and ueberschuss >= 3200:
        return {
            "status": "HEIZSTAB_3KW",
            "titel": "🔥 PV Hinweis",
            "prioritaet": 0,
            "nachricht": (
                f"3 kW Heizstab sinnvoll\n\n"
                f"Batterie: {batterie}%\n"
                f"PV-Leistung: {pv} W\n"
                f"Hausverbrauch: {haus} W\n"
                f"Überschuss: {ueberschuss} W\n"
                f"Netzbezug: {netz} W"
            ),
        }

    return {
        "status": "KEINE_AKTION",
        "titel": "ℹ️ PV Status",
        "prioritaet": -1,
        "nachricht": (
            f"Keine Aktion erforderlich\n\n"
            f"Batterie: {batterie}%\n"
            f"PV-Leistung: {pv} W\n"
            f"Hausverbrauch: {haus} W\n"
            f"Überschuss: {ueberschuss} W\n"
            f"Netzbezug: {netz} W"
        ),
    }


def konsolen_ausgabe(zeit: str, ergebnis: dict, letzter_status: str) -> None:
    """
    Übersichtliche Ausgabe für GitHub Logs.
    """
    print("=== PV MONITOR START ===")
    print(f"Zeit: {zeit}")
    print(f"Letzter Status: {letzter_status}")
    print(f"Neuer Status:   {ergebnis['status']}")
    print("-" * 60)
    print(ergebnis["nachricht"])
    print("-" * 60)


def main() -> None:
    zeit = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    gespeicherter_status = lade_status()
    letzter_status = gespeicherter_status.get("letzter_status", "")

    app_key = os.environ.get("ISOLARCLOUD_APP_KEY")
    secret_key = os.environ.get("SOLARCLOUD_SECRET_KEY")
    user_account = os.environ.get("ISOLARCLOUD_USER_ACCOUNT")
    user_password = os.environ.get("ISOLARCLOUD_USER_PASSWORD")

    if not all([app_key, secret_key, user_account, user_password]):
        print("❌ iSolarCloud Zugangsdaten fehlen!")
        print("APP_KEY vorhanden:", bool(app_key))
        print("SECRET_KEY vorhanden:", bool(secret_key))
        print("USER_ACCOUNT vorhanden:", bool(user_account))
        print("USER_PASSWORD vorhanden:", bool(user_password))
        return

    daten = hole_isolarcloud_daten(app_key, secret_key, user_account, user_password)

    if daten is None:
        print("❌ Keine Daten von iSolarCloud erhalten. Durchlauf wird abgebrochen.")
        return

    ergebnis = bewerte_status(daten)

    konsolen_ausgabe(zeit, ergebnis, letzter_status)

    if ergebnis["status"] != letzter_status:
        print("✅ Statusänderung erkannt -> Push wird gesendet.")

        nachricht_gesamt = f"Zeit: {zeit}\n\n{ergebnis['nachricht']}"

        sende_pushover(
            titel=ergebnis["titel"],
            nachricht=nachricht_gesamt,
            prioritaet=ergebnis["prioritaet"],
        )

        speichere_status(
            {
                "letzter_status": ergebnis["status"],
                "letzter_status_text": ergebnis["nachricht"].split("\n")[0],
                "letzte_aktualisierung": zeit,
                "letzte_push_zeit": zeit,
            }
        )
    else:
        print("ℹ️ Kein Statuswechsel -> keine neue Push-Nachricht.")

    print("=== PV MONITOR ENDE ===")


if __name__ == "__main__":
    main()
