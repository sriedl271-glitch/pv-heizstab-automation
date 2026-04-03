from datetime import datetime


def send_pushover(title: str, message: str) -> None:
    """
    Test-Platzhalter.
    Später bauen wir hier die echte Pushover-Anbindung ein.
    """
    print("PUSHOVER TEST")
    print(f"TITEL: {title}")
    print("NACHRICHT:")
    print(message)
    print("-" * 50)


def send_email(subject: str, body: str) -> None:
    """
    Test-Platzhalter.
    Später bauen wir hier die echte E-Mail-Anbindung ein.
    """
    print("EMAIL TEST")
    print(f"BETREFF: {subject}")
    print("INHALT:")
    print(body)
    print("-" * 50)


def evaluate_test_data() -> dict:
    """
    Testdaten für den ersten Start.
    Später werden diese Werte durch echte iSolarCloud-Daten ersetzt.
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
    Erstellt aus den Testdaten eine erste einfache Entscheidung.
    """
    battery = data["battery_percent"]
    pv_power = data["pv_power_w"]
    house_power = data["house_power_w"]
    grid = data["grid_power_w"]
    surplus = data["surplus_w"]

    if grid > 200:
        title = "PV Warnung"
        message = (
            f"Netzbezug erkannt.\n"
            f"Batterie: {battery}%\n"
            f"PV-Leistung: {pv_power} W\n"
            f"Hausverbrauch: {house_power} W\n"
            f"Überschuss: {surplus} W\n"
            f"Netzbezug: {grid} W\n"
            f"Empfehlung: Heizstab ausschalten."
        )
    elif battery >= 94 and surplus >= 6300:
        title = "PV Hinweis"
        message = (
            f"6 kW sinnvoll.\n"
            f"Batterie: {battery}%\n"
            f"PV-Leistung: {pv_power} W\n"
            f"Hausverbrauch: {house_power} W\n"
            f"Überschuss: {surplus} W\n"
            f"Netzbezug: {grid} W"
        )
    elif battery >= 88 and surplus >= 3200:
        title = "PV Hinweis"
        message = (
            f"3 kW sinnvoll.\n"
            f"Batterie: {battery}%\n"
            f"PV-Leistung: {pv_power} W\n"
            f"Hausverbrauch: {house_power} W\n"
            f"Überschuss: {surplus} W\n"
            f"Netzbezug: {grid} W"
        )
    else:
        title = "PV Status"
        message = (
            f"Aktuell keine Aktion.\n"
            f"Batterie: {battery}%\n"
            f"PV-Leistung: {pv_power} W\n"
            f"Hausverbrauch: {house_power} W\n"
            f"Überschuss: {surplus} W\n"
            f"Netzbezug: {grid} W"
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
