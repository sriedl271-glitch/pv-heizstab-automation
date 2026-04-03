import os
import requests
from datetime import datetime


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
        response = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=daten,
            timeout=10,
        )
        print("Pushover Statuscode:", response.status_code)
        print("Pushover Antwort:", response.text)
    except Exception as e:
        print("❌ Fehler bei Pushover:", str(e))


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
    Wird später durch echte iSolarCloud-Daten ersetzt.
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

    # 1. Wichtigster Fall: Netzbezug
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

    # 2. 6 kW sinnvoll
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

    # 3. 3 kW sinnvoll
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

    # 4. Keine Aktion nötig
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


def konsolen_ausgabe(zeit: str, ergebnis: dict) -> None:
    """
    Übersichtliche Ausgabe für GitHub Logs.
    """
    print("=== PV MONITOR START ===")
    print(f"Zeit: {zeit}")
    print(f"Status: {ergebnis['status']}")
    print("-" * 60)
    print(ergebnis["nachricht"])
    print("-" * 60)


def main() -> None:
    zeit = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    daten = hole_testdaten()
    ergebnis = bewerte_status(daten)

    nachricht_gesamt = f"Zeit: {zeit}\n\n{ergebnis['nachricht']}"

    konsolen_ausgabe(zeit, ergebnis)

    sende_pushover(
        titel=ergebnis["titel"],
        nachricht=nachricht_gesamt,
        prioritaet=ergebnis["prioritaet"],
    )

    sende_email(
        betreff=ergebnis["titel"],
        inhalt=nachricht_gesamt,
    )

    print("=== PV MONITOR ENDE ===")


if __name__ == "__main__":
    main()
