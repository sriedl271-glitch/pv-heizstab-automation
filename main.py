import json
import os
from datetime import datetime

import requests

STATUS_DATEI = "status.json"


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
    Diese Funktion ersetzen wir später durch echte iSolarCloud-Daten.
    """
    return {
        "batterie_prozent": 92,
        "pv_leistung_w": 5200,
        "hausverbrauch_w": 1400,
        "netzbezug_w": 0,
        "ueberschuss_w": 3800,
    }


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

    daten = hole_testdaten()
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
