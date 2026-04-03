import os
import requests
from datetime import datetime


def send_pushover(title: str, message: str) -> None:
    """
    Sendet eine echte Push-Nachricht über Pushover.
    """
    user_key = os.environ.get("PUSHOVER_USER_KEY")
    api_token = os.environ.get("PUSHOVER_API_TOKEN")

    if not user_key or not api_token:
        print("❌ Pushover Keys fehlen!")
        return

    data = {
        "token": api_token,
        "user": user_key,
        "title": title,
        "message": message,
    }

    try:
        response = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=data,
            timeout=10,
        )
        print(f"✅ Pushover gesendet: {response.status_code}")
    except Exception as e:
        print("❌ Fehler bei Pushover:", str(e))


def send_email(subject: str, body: str) -> None:
    """
    Aktuell nur Platzhalter.
    (bauen wir später richtig ein)
    """
    print("📧 EMAIL TEST")
    print(f"BETREFF: {subject}")
    print("INHALT:")
    print(body)
    print("-" * 50)


def evaluate_test_data() -> dict:
    """
    Testdaten (später iSolarCloud).
    """
    return {
        "battery_percent": 92,
        "pv_power_w": 5200,
        "house_power_w": 1400,
        "grid_power_w": 0,
        "surplus_w": 3800,
    }


def build_message(data: dict) -> tuple[str, str]:
    """
    Logik zur Entscheidung.
    """
    battery = data["battery_percent"]
    pv_power = data["pv_power_w"]
    house_power = data["house_power_w"]
    grid = data["grid_power_w"]
    surplus = data["surplus_w"]

    if grid > 200:
        title = "PV Warnung"
        message = (
            f"⚠️ Netzbezug erkannt!\n\n"
            f"Batterie: {battery}%\n"
            f"PV-Leistung: {pv_power} W\n"
            f"Hausverbrauch: {house_power} W\n"
            f"Überschuss: {surplus} W\n"
            f"Netzbezug: {grid} W\n\n"
            f"👉 Empfehlung: Heizstab AUS"
        )

    elif battery >= 94 and surplus >= 6300:
        title = "PV Hinweis"
        message = (
            f"🔥 6 kW sinnvoll\n\n"
            f"Batterie: {battery}%\n"
            f"PV-Leistung: {pv_power} W\n"
            f"Hausverbrauch: {house_power} W\n"
            f"Überschuss: {surplus} W"
        )

    elif battery >= 88 and surplus >= 3200:
        title = "PV Hinweis"
        message = (
            f"🔥 3 kW sinnvoll\n\n"
            f"Batterie: {battery}%\n"
            f"PV-Leistung: {pv_power} W\n"
            f"Hausverbrauch: {house_power} W\n"
            f"Überschuss: {surplus} W"
        )

    else:
        title = "PV Status"
        message = (
            f"ℹ️ Keine Aktion nötig\n\n"
            f"Batterie: {battery}%\n"
            f"PV-Leistung: {pv_power} W\n"
            f"Hausverbrauch: {house_power} W\n"
            f"Überschuss: {surplus} W"
        )

    return title, message


def main() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    data = evaluate_test_data()
    title, message = build_message(data)

    full_message = f"Zeit: {now}\n\n{message}"

    print("=== PV MONITOR START ===")
    print(full_message)
    print("-" * 50)

    send_pushover(title, full_message)
    send_email(title, full_message)

    print("=== PV MONITOR ENDE ===")


if __name__ == "__main__":
    main()
