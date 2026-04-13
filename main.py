import json
import os
from datetime import datetime

import requests

STATUS_DATEI = "status.json"
ISOLARCLOUD_API_URL = "https://gateway.isolarcloud.eu"

# Messpunkt-IDs für iSolarCloud Echtdaten
MESSPUNKT_BATTERIE_SOC = "83252"  # Batterie-Ladestand (SOC) in %
MESSPUNKT_PV_LEISTUNG = "83067"   # PV-Gesamtleistung in W
MESSPUNKT_HAUSVERBRAUCH = "83106" # Hausverbrauch (Load Power) in W
MESSPUNKT_NETZ = "83549"          # Netzleistung in W (positiv = Bezug, negativ = Einspeisung)


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


def hole_isolarcloud_daten(app_key: str, secret_key: str, user_account: str, user_password: str):
    """
    Holt Echtdaten von iSolarCloud.
    Gibt ein Dict mit Messwerten zurück, oder None bei Fehler.
    """
    token = isolarcloud_login(app_key, secret_key, user_account, user_password)
    if not token:
        return None

    ps_id = isolarcloud_get_ps_id(app_key, secret_key, token)
    if not ps_id:
        return None

    url = f"{ISOLARCLOUD_API_URL}/openapi/platform/getPowerStationRealTimeData"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "x-access-key": secret_key,
    }
    body = {
        "appkey": app_key,
        "Authorization": f"Bearer {token}",
        "ps_id_list": [ps_id],
        "point_id_list": [
            MESSPUNKT_BATTERIE_SOC,
            MESSPUNKT_PV_LEISTUNG,
            MESSPUNKT_HAUSVERBRAUCH,
            MESSPUNKT_NETZ,
        ],
    }
    try:
        antwort = requests.post(url, json=body, headers=headers, timeout=15)
        daten = antwort.json()
        if daten.get("result_code") == "1":
            punkte = daten["result_data"]["device_point_list"][0]

            batterie = int(round(float(punkte.get(f"p{MESSPUNKT_BATTERIE_SOC}") or 0)))
            pv = int(round(float(punkte.get(f"p{MESSPUNKT_PV_LEISTUNG}") or 0)))
            haus = int(round(float(punkte.get(f"p{MESSPUNKT_HAUSVERBRAUCH}") or 0)))
            netz_roh = float(punkte.get(f"p{MESSPUNKT_NETZ}") or 0)

            netzbezug = int(round(max(0.0, netz_roh)))
            ueberschuss = int(round(max(0.0, pv - haus)))

            print(f"✅ iSolarCloud Echtdaten empfangen:")
            print(f"   Batterie: {batterie}%")
            print(f"   PV-Leistung: {pv} W")
            print(f"   Hausverbrauch: {haus} W")
            print(f"   Netzleistung (roh): {netz_roh} W")
            print(f"   Netzbezug: {netzbezug} W")
            print(f"   Überschuss: {ueberschuss} W")

            return {
                "batterie_prozent": batterie,
                "pv_leistung_w": pv,
                "hausverbrauch_w": haus,
                "netzbezug_w": netzbezug,
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
