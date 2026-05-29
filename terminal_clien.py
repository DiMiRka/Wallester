from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

from wallester_cards import DEFAULT_BASE_URL, DEFAULT_EXPORT_FIELDS, WallesterClient, WallesterError, load_env_file


ACCOUNT_OPTIONS = [
    ("430263 (Евро)", "51129252"),
    ("446616 (Доллар)", "95107504"),
]

CLOSE_REASONS = [
    "ClosedByClient",
    "ClosedByCardholder",
    "ClosedByIssuer",
    "ClosedBySystem",
    "ClosedByReplace",
]

DEFAULT_SECURITY = {
    "internet_purchase_enabled": True,
    "contactless_enabled": False,
    "withdrawal_enabled": False,
    "all_time_limits_enabled": False,
}


class BackToMenu(Exception):
    """User requested return to the main menu."""


def setup_terminal() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def pause() -> None:
    input("\nНажмите Enter, чтобы продолжить...")


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix} (q - в меню): ").strip()
    if value.lower() == "q":
        raise BackToMenu
    return value or (default or "")


def ask_optional(prompt: str, default: str | None = None) -> str | None:
    value = ask(prompt, default)
    return value or None


def ask_int(prompt: str, default: int, minimum: int = 0) -> int:
    raw = ask(prompt, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{prompt}: нужно указать число.") from exc
    if value < minimum:
        raise ValueError(f"{prompt}: значение должно быть не меньше {minimum}.")
    return value


def ask_bool(prompt: str, default: bool | None = None) -> bool | None:
    default_hint = ""
    if default is True:
        default_hint = " [1]"
    elif default is False:
        default_hint = " [0]"
    value = input(f"{prompt} (1 - да / 0 - нет{default_hint}, q - в меню): ").strip().lower()
    if value == "q":
        raise BackToMenu
    if not value and default is not None:
        return default
    if not value:
        return None
    if value == "1":
        return True
    if value == "0":
        return False
    print("Введите 1 для 'да' или 0 для 'нет'.")
    return None


def choose(title: str, options: list[str], default_index: int = 0) -> str:
    print(title)
    for idx, option in enumerate(options, start=1):
        print(f"{idx}. {option}")
    raw = ask("Выберите номер", str(default_index + 1))
    try:
        index = int(raw) - 1
    except ValueError:
        index = default_index
    if index < 0 or index >= len(options):
        index = default_index
    return options[index]


def choose_account_id() -> str:
    label = choose("Выберите счет", [label for label, _account_id in ACCOUNT_OPTIONS])
    for option_label, account_id in ACCOUNT_OPTIONS:
        if option_label == label:
            return account_id
    return ACCOUNT_OPTIONS[0][1]


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def save_if_needed(data: Any) -> None:
    output = ask_optional("Сохранить полный ответ API в файл? Укажите путь или оставьте пустым")
    if not output:
        return
    Path(output).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Сохранено: {output}")


def card_summary(data: dict[str, Any]) -> None:
    card = data.get("card", data)
    if not isinstance(card, dict):
        print_json(data)
        return
    fields = [
        "id",
        "name",
        "status",
        "type",
        "is_disposable",
        "masked_card_number",
        "account_id",
        "person_id",
        "company_id",
        "currency_code",
        "expiry_date",
        "created_at",
        "updated_at",
    ]
    for field in fields:
        value = card.get(field)
        if value not in (None, ""):
            print(f"{field}: {value}")


def build_client() -> WallesterClient:
    load_env_file(Path(".env"))
    base_url = os.getenv("BASE_URL", DEFAULT_BASE_URL)
    token = os.getenv("TOKEN")
    if not token:
        raise SystemExit("TOKEN не задан. Добавьте его в .env.")
    return WallesterClient(base_url=base_url, token=token)


def get_3ds_defaults() -> tuple[str, str]:
    email = os.getenv("3DS_EMAIL", "").strip()
    password = os.getenv("3DS_PASSWORD", "").strip()
    missing = []
    if not email:
        missing.append("3DS_EMAIL")
    if not password:
        missing.append("3DS_PASSWORD")
    if missing:
        raise ValueError("В .env не заданы значения: " + ", ".join(missing))
    return email, password


def build_expense_virtual_payload(account_id: str, name: str) -> dict[str, Any]:
    email, password = get_3ds_defaults()
    return {
        "type": "Virtual",
        "is_disposable": False,
        "account_id": account_id,
        "name": name,
        "security": DEFAULT_SECURITY.copy(),
        "3d_secure_settings": {
            "email": email,
            "password": password,
        },
    }


def issue_card(client: WallesterClient) -> None:
    print("\nВыпуск Expense Virtual Card")
    account_id = choose_account_id()
    count = ask_int("Количество карт", 1, minimum=1)
    payloads = [
        build_expense_virtual_payload(account_id, f"Auto card {idx:03d}")
        for idx in range(1, count + 1)
    ]

    print("\nПервый payload:")
    print_json(payloads[0])
    if count > 1:
        print(f"\nВсего будет выпущено карт: {count}")
        print(f"Имена: Auto card 001 ... Auto card {count:03d}")

    if not ask_bool("Выпустить карты?", False):
        print("Операция отменена.")
        return

    results = []
    for index, payload in enumerate(payloads, start=1):
        print(f"\nВыпуск {index}/{count}: {payload['name']}")
        result = client.create_card(payload, send_notification=False)
        results.append(result)
        card_summary(result)

    save_if_needed({"issued_cards": results})


def get_card(client: WallesterClient) -> None:
    print("\nПолучение данных по карте")
    card_id = ask("Card ID")
    result = client.get_card(card_id)
    if ask_bool("Получить зашифрованные PAN/CVV2/PIN/3DS?", False):
        public_key_path = ask("Путь к файлу с base64 RSA public key")
        public_key = Path(public_key_path).read_text(encoding="utf-8").strip()
        encrypted: dict[str, Any] = {}
        for kind in ["number", "cvv2", "pin", "3ds"]:
            if ask_bool(f"Получить {kind}?", kind in {"number", "cvv2"}):
                encrypted[kind] = client.encrypted_value(card_id, kind, public_key)
        result["encrypted"] = encrypted
    print()
    card_summary(result)
    if ask_bool("Показать полный JSON?", False):
        print_json(result)
    save_if_needed(result)


def export_cards(client: WallesterClient) -> None:
    print("\nВыгрузка карт")
    query = {
        "masked_card_number": ask_optional("Маскированный номер карты"),
        "reference_number": ask_optional("Reference number"),
        "external_id": ask_optional("External ID"),
        "from_record": ask_int("Начальная запись from_record", 0, minimum=0),
        "records_count": ask_int("Количество записей records_count", 100, minimum=1),
    }
    result = client.search_cards(query)
    cards = result.get("cards", [])
    print(f"\nЗаписей в ответе: {len(cards)}")
    print(f"Всего записей по фильтру: {result.get('total_records_number', 'не указано')}")
    for card in cards[:10]:
        print("- " + " | ".join(str(card.get(field, "")) for field in ["id", "name", "status", "masked_card_number"]))
    if len(cards) > 10:
        print(f"... еще {len(cards) - 10}")

    output = ask_optional("Сохранить выгрузку в файл? Укажите путь или оставьте пустым")
    if output:
        if output.lower().endswith(".csv"):
            with open(output, "w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=DEFAULT_EXPORT_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(cards)
        else:
            Path(output).write_text(json.dumps(cards, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Сохранено: {output}")


def rename_card(client: WallesterClient) -> None:
    print("\nПереименование карты")
    card_id = ask("Card ID")
    new_name = ask("Новое название карты")
    result = client.rename_card(card_id, new_name)
    print("\nКарта переименована:")
    card_summary(result)
    save_if_needed(result)


def close_card(client: WallesterClient) -> None:
    print("\nЗакрытие карты")
    card_id = ask("Card ID")
    reason = choose("Причина закрытия", CLOSE_REASONS)
    print(f"\nБудет закрыта карта: {card_id}")
    print(f"Причина: {reason}")
    confirm = ask("Для подтверждения введите CLOSE")
    if confirm != "CLOSE":
        print("Операция отменена.")
        return
    result = client.close_card(card_id, reason)
    print("\nКарта закрыта:")
    card_summary(result)
    save_if_needed(result)


def show_menu() -> str:
    clear_screen()
    print("Терминальный клиент Wallester")
    print("=" * 31)
    print("1. Выпустить Expense Virtual Card")
    print("2. Получить данные по карте")
    print("3. Выгрузить карты")
    print("4. Переименовать карту")
    print("5. Закрыть карту")
    print("q. Выход")
    return input("Выберите действие: ").strip().lower()


def main() -> int:
    setup_terminal()
    client = build_client()
    actions = {
        "1": issue_card,
        "2": get_card,
        "3": export_cards,
        "4": rename_card,
        "5": close_card,
    }
    while True:
        choice = show_menu()
        if choice == "q":
            return 0
        action = actions.get(choice)
        if not action:
            print("Неизвестный пункт меню.")
            pause()
            continue
        try:
            action(client)
        except BackToMenu:
            continue
        except (WallesterError, ValueError, OSError, json.JSONDecodeError) as exc:
            print(f"\nОшибка: {exc}")
            pause()


if __name__ == "__main__":
    raise SystemExit(main())
