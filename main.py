import os
import requests
from datetime import datetime


def send_pushover(title: str, message: str, priority: int = 0) -> None:
    """
    Sendet eine Push-Nachricht über Pushover.
    priority:
        -2 = lautlos
        -1 = unauffällig
         0 = normal
         1 = wichtig
    """
    user_key = os.environ.get("PUSHOVER_USER_KEY")
    api_token = os.environ.get("PUSHOVER_API_TOKEN")

    if not user_key or not api_token:
        print("❌ Pushover Keys fehlen!")
        print("USER_KEY vorhanden:", bool(user_key))
        print("API_TOKEN vorhanden:", bool(api_token))
        return

    data = {
        "token": api_token,
        "user": user_key,
        "title": title,
        "message": message,
        "priority": priority,
    }

    try:
        response = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=data,
            timeout=10,
        )
        print("Pushover Statuscode:", response.status_code)
        print("Pushover Antwort:", response.text)
    except Exception as e:
        print("❌ Fehler bei Pushover:", str(e))


def send_email(subject: str, body: str) -> None:
    """
    Platzhalter für späteren E-Mail-Versand.
    """
    print("📧 EMAIL TEST")
    print(f"BETREFF: {subject}")
    print("INHALT:")
    print(body)
    print("-" * 60)


def get_test_data() -> dict:
    """
    Testdaten für die Entwicklung.
    Diese Funktion ersetzen wir später durch echte iSolarCloud-Daten.
    """
    return {
        "battery_percent": 92,
        "pv_power_w": 5200,
        "house_power_w": 1400,
        "grid_power_w": 0,
        "surplus_w": 3800,
    }


def evaluate_status(data: dict) -> dict:
    """
    Bewertet die aktuelle Lage anhand der Daten
    und gibt einen sauberen Status zurück.
    """
    battery = data["battery_percent"]
    pv_power = data["pv_power_w"]
    house_power = data["house_power_w"]
    grid = data["grid_power_w"]
    surplus = data["surplus_w"]

    # Priorität: Netzbezug zuerst prüfen
    if grid > 200:
        return {
            "status_code": "GRID_WARNING",
            "title": "PV Warnung",
            "priority": 1,
            "message": (
                f"⚠️ Netzbezug erkannt\n\n"
                f"Batterie: {battery}%\n"
                f"PV-Leistung: {pv_power} W\n"
                f"Hausverbrauch: {house_power} W\n"
                f"Überschuss: {surplus} W\n"
                f"Netzbezug: {grid} W\n\n"
                f"👉 Empfehlung: Heizstab ausschalten"
            ),
        }

    if battery >= 94 and surplus >= 6300:
        return {
            "status_code": "HEAT_6KW",
            "title": "PV Hinweis",
            "priority": 0,
            "message": (
                f"🔥 6 kW sinnvoll\n\n"
                f"Batterie: {battery}%\n"
                f"PV-Leistung: {pv_power} W\n"
                f"Hausverbrauch: {house_power} W\n"
                f"Überschuss: {surplus} W\n"
                f"Netzbezug: {grid} W"
            ),
        }

    if battery >= 88 and surplus >= 3200:
        return {
            "status_code": "HEAT_3KW",
            "title": "PV Hinweis",
            "priority": 0,
            "message": (
                f"🔥 3 kW sinnvoll\n\n"
                f"Batterie: {battery}%\n"
                f"PV-Leistung: {pv_power} W\n"
                f"Hausverbrauch: {house_power} W\n"
                f"Überschuss: {surplus} W\n"
                f"Netzbezug: {grid} W"
            ),
        }

    return {
        "status_code": "NO_ACTION",
        "title": "PV Status",
        "priority": -1,
        "message": (
            f"ℹ️ Keine Aktion nötig\n\n"
            f"Batterie: {battery}%\n"
            f"PV-Leistung: {pv_power} W\n"
            f"Hausverbrauch: {house_power} W\n"
            f"Überschuss: {surplus} W\n"
            f"Netzbezug: {grid} W"
        ),
    }


def print_console_block(now: str, result: dict) -> None:
    """
    Schöne Konsolenausgabe für GitHub Actions.
    """
    print("=== PV MONITOR START ===")
    print(f"Zeit: {now}")
    print(f"Status: {result['status_code']}")
    print("-" * 60)
    print(result["message"])
    print("-" * 60)


def main() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    data = get_test_data()
    result = evaluate_status(data)

    full_message = f"Zeit: {now}\n\n{result['message']}"

    print_console_block(now, result)

    send_pushover(
        title=result["title"],
        message=full_message,
        priority=result["priority"],
    )

    send_email(
        subject=result["title"],
        body=full_message,
    )

    print("=== PV MONITOR ENDE ===")


if __name__ == "__main__":
    main()
