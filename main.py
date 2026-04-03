import os
import requests
from datetime import datetime

STATUS_DATEI = "status.txt"


def lade_letzten_status():
    try:
        with open(STATUS_DATEI, "r") as f:
            return f.read().strip()
    except:
        return None


def speichere_status(status):
    with open(STATUS_DATEI, "w") as f:
        f.write(status)


def sende_pushover(titel, nachricht, prioritaet=0):
    user_key = os.environ.get("PUSHOVER_USER_KEY")
    api_token = os.environ.get("PUSHOVER_API_TOKEN")

    if not user_key or not api_token:
        print("❌ Pushover-Zugangsdaten fehlen!")
        return

    data = {
        "token": api_token,
        "user": user_key,
        "title": titel,
        "message": nachricht,
        "priority": prioritaet,
    }

    try:
        response = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=data,
            timeout=10,
        )
        print("Pushover gesendet:", response.status_code)
    except Exception as e:
        print("❌ Fehler:", str(e))


def hole_testdaten():
    return {
        "batterie": 92,
        "pv": 5200,
        "haus": 1400,
        "netz": 0,
        "ueberschuss": 3800,
    }


def bewerte_status(d):
    if d["netz"] > 200:
        return "NETZ", "⚠️ Netzbezug!", 1

    if d["batterie"] >= 94 and d["ueberschuss"] >= 6300:
        return "6KW", "🔥 6 kW sinnvoll", 0

    if d["batterie"] >= 88 and d["ueberschuss"] >= 3200:
        return "3KW", "🔥 3 kW sinnvoll", 0

    return "IDLE", "ℹ️ Keine Aktion nötig", -1


def main():
    jetzt = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    daten = hole_testdaten()
    status, text, prioritaet = bewerte_status(daten)

    letzter_status = lade_letzten_status()

    print("Alter Status:", letzter_status)
    print("Neuer Status:", status)

    if status != letzter_status:
        print("➡️ Status geändert → sende Push")

        nachricht = f"{text}\n\nZeit: {jetzt}"

        sende_pushover("PV Status", nachricht, prioritaet)
        speichere_status(status)

    else:
        print("⏸️ Kein neuer Status → keine Nachricht")

    print("=== ENDE ===")


if __name__ == "__main__":
    main()
