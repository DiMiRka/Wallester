from __future__ import annotations

import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from wallester_cards import (
    DEFAULT_BASE_URL,
    DEFAULT_USER_AGENT,
    JWTError,
    WallesterClient,
    WallesterError,
    WallesterJWTProvider,
    load_env_file,
    rsa_decrypt_text,
    rsa_public_key_pem_base64,
)


ACCOUNT_OPTIONS: list[dict[str, str]] = [
    {
        "label": "430263 (Евро)",
        "lookup": "51129252",
        "display_number": "430263",
        "currency_code": "EUR",
    },
    {
        "label": "446616 (Доллар)",
        "lookup": "95107504",
        "display_number": "446616",
        "currency_code": "USD",
    },
]

CLOSE_REASON_OPTIONS: list[dict[str, str]] = [
    {"label": "Карта не актуальна", "value": "ClosedByClient"},
    {"label": "Закрыта держателем карты", "value": "ClosedByCardholder"},
    {"label": "Закрыта эмитентом", "value": "ClosedByIssuer"},
    {"label": "Закрыта системой", "value": "ClosedBySystem"},
    {"label": "Закрыта из-за замены", "value": "ClosedByReplace"},
]

DEFAULT_SECURITY = {
    "internet_purchase_enabled": True,
    "contactless_enabled": False,
    "withdrawal_enabled": False,
    "all_time_limits_enabled": False,
}
DEFAULT_RENAME_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1p_qTT1JJrR7zhTUqap5q_ZI9rD6kkqLUBfzVTzhaQPY/"
    "edit?pli=1&gid=844684271#gid=844684271"
)
DEFAULT_CLOSE_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1p_qTT1JJrR7zhTUqap5q_ZI9rD6kkqLUBfzVTzhaQPY/"
    "edit?pli=1&gid=1502755584#gid=1502755584"
)
CARD_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


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


def choose_account() -> dict[str, str]:
    label = choose("Выберите счет", [option["label"] for option in ACCOUNT_OPTIONS])
    for option in ACCOUNT_OPTIONS:
        if option["label"] == label:
            return option
    return ACCOUNT_OPTIONS[0]


def resolve_account_id(client: WallesterClient, account: dict[str, str]) -> str:
    lookup = account["lookup"]
    display_number = account["display_number"]
    currency_code = account["currency_code"]
    response = client.search_accounts(
        {
            "from_record": 0,
            "records_count": 1000,
        }
    )
    accounts = response.get("accounts", []) if isinstance(response, dict) else []
    matches = []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        values = {
            str(item.get("id", "")),
            str(item.get("name", "")),
            str(item.get("external_id", "")),
            str(item.get("reference_number", "")),
        }
        same_lookup = lookup in values or display_number in values
        same_currency = item.get("currency_code") == currency_code
        if same_lookup and same_currency:
            matches.append(item)

    if not matches:
        raise ValueError(
            "Не удалось найти UUID счета для "
            f"{account['label']} по значению {lookup}. "
            "В Wallester API поле account_id должно быть UUID, а не короткий номер счета."
        )
    if len(matches) > 1:
        active_matches = [item for item in matches if item.get("status") == "Active"]
        if len(active_matches) == 1:
            return str(active_matches[0]["id"])
        raise ValueError(
            "Найдено несколько подходящих счетов для "
            f"{account['label']}. Нужно уточнить UUID счета вручную."
        )

    account_id = matches[0].get("id")
    if not account_id:
        raise ValueError(f"У найденного счета {account['label']} нет поля id.")
    return str(account_id)


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def extract_card(data: dict[str, Any]) -> dict[str, Any]:
    card = data.get("card", data)
    return card if isinstance(card, dict) else {}


def build_client() -> WallesterClient:
    load_env_file(Path(".env"))
    base_url = os.getenv("BASE_URL", DEFAULT_BASE_URL)
    user_agent = os.getenv("USER_AGENT", DEFAULT_USER_AGENT)
    api_key = os.getenv("API_KEY", "").strip()
    private_key_path = os.getenv("PRIVATE_KEY_PATH", "").strip()
    if not api_key or not private_key_path:
        raise SystemExit("В .env должны быть заданы API_KEY и PRIVATE_KEY_PATH.")
    jwt_provider = WallesterJWTProvider(
        api_key=api_key,
        private_key_path=private_key_path,
    )
    return WallesterClient(base_url=base_url, jwt_provider=jwt_provider, user_agent=user_agent)


def get_private_key_path() -> str:
    private_key_path = os.getenv("CARD_DATA_PRIVATE_KEY_PATH", "").strip()
    if private_key_path:
        return private_key_path
    private_key_path = os.getenv("PRIVATE_KEY_PATH", "").strip()
    if not private_key_path:
        raise ValueError("В .env не задан CARD_DATA_PRIVATE_KEY_PATH или PRIVATE_KEY_PATH.")
    return private_key_path


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


def encrypted_field(response: Any, field_name: str) -> str:
    if not isinstance(response, dict):
        raise ValueError(f"Wallester не вернул поле {field_name}.")
    value = response.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Wallester не вернул поле {field_name}.")
    return value


def format_expiry_date(card: dict[str, Any]) -> str:
    value = card.get("expiry_date") or card.get("expiration_date") or card.get("expires_at")
    if not value:
        return "срок не найден"
    text = str(value)
    if len(text) >= 7 and text[4] == "-":
        return f"{text[5:7]}/{text[2:4]}"
    return text


def card_sensitive_line(client: WallesterClient, card_id: str, card: dict[str, Any]) -> str:
    private_key_path = get_private_key_path()
    public_key = rsa_public_key_pem_base64(private_key_path)
    encrypted_number = encrypted_field(
        client.encrypted_value(card_id, "number", public_key),
        "encrypted_card_number",
    )
    encrypted_cvv = encrypted_field(
        client.encrypted_value(card_id, "cvv2", public_key),
        "encrypted_cvv2",
    )
    number = rsa_decrypt_text(encrypted_number, private_key_path)
    cvv = rsa_decrypt_text(encrypted_cvv, private_key_path)
    return f"{number}/{format_expiry_date(card)}/{cvv}"


def issue_card(client: WallesterClient) -> None:
    print("\nВыпуск Expense Virtual Card")
    account = choose_account()
    print(f"Ищу UUID счета для {account['label']}...")
    account_id = resolve_account_id(client, account)
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
        card = extract_card(result)
        card_id = card.get("id")
        if not card_id:
            raise ValueError("Wallester не вернул ID выпущенной карты.")
        try:
            print(card_sensitive_line(client, str(card_id), card))
        except (JWTError, WallesterError, ValueError) as exc:
            print(f"Карта выпущена, но данные номер/срок/cvv не удалось получить: {exc}")

    print("\nКарты успешно выпущены")
    print("=" * 24)
    for result in results:
        card = extract_card(result)
        name = card.get("name") or "Без названия"
        card_id = card.get("id") or "ID не найден"
        print(f"- {name}: {card_id}")


def google_sheet_csv_url(sheet_url: str) -> str:
    parsed = urllib.parse.urlparse(sheet_url)
    match = re.search(r"/spreadsheets/d/([^/]+)", parsed.path)
    if not match:
        raise ValueError("Не удалось определить ID Google Sheets из ссылки.")
    query = urllib.parse.parse_qs(parsed.query)
    gid = query.get("gid", ["0"])[0]
    if parsed.fragment.startswith("gid="):
        gid = parsed.fragment.removeprefix("gid=")
    return f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=csv&gid={gid}"


def load_rename_rows(sheet_url: str) -> list[dict[str, str]]:
    csv_url = google_sheet_csv_url(sheet_url)
    request = urllib.request.Request(
        csv_url,
        headers={"User-Agent": os.getenv("USER_AGENT", DEFAULT_USER_AGENT)},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Не удалось скачать таблицу: HTTP {exc.code}. Проверьте доступ по ссылке.") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Не удалось скачать таблицу: {exc.reason}") from exc

    reader = csv.DictReader(content.splitlines())
    if not reader.fieldnames:
        raise ValueError("В таблице не найдены заголовки.")
    missing_columns = [column for column in ["Cards", "Имя баера"] if column not in reader.fieldnames]
    if missing_columns:
        raise ValueError("В таблице не найдены столбцы: " + ", ".join(missing_columns))

    rows = []
    for row in reader:
        card_value = (row.get("Cards") or "").strip()
        buyer_name = (row.get("Имя баера") or "").strip()
        if card_value and buyer_name:
            rows.append({"card_value": card_value, "name": buyer_name})
    return rows


def load_card_values(sheet_url: str) -> list[str]:
    csv_url = google_sheet_csv_url(sheet_url)
    request = urllib.request.Request(
        csv_url,
        headers={"User-Agent": os.getenv("USER_AGENT", DEFAULT_USER_AGENT)},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Не удалось скачать таблицу: HTTP {exc.code}. Проверьте доступ по ссылке.") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Не удалось скачать таблицу: {exc.reason}") from exc

    reader = csv.DictReader(content.splitlines())
    if not reader.fieldnames:
        raise ValueError("В таблице не найдены заголовки.")
    if "Cards" not in reader.fieldnames:
        raise ValueError("В таблице не найден столбец: Cards")

    values = []
    for row in reader:
        card_value = (row.get("Cards") or "").strip()
        if card_value:
            values.append(card_value)
    return values


def card_number_digits(card_value: str) -> str:
    number_part = card_value.split("/", 1)[0]
    return "".join(char for char in number_part if char.isdigit())


def masked_number_from_card_number(card_number: str) -> str:
    if len(card_number) < 10:
        raise ValueError(f"в столбце Cards должен быть полный номер карты или Card ID, получено: {card_number}")
    return f"{card_number[:6]}******{card_number[-4:]}"


def decrypted_card_number(client: WallesterClient, card_id: str) -> str:
    private_key_path = get_private_key_path()
    public_key = rsa_public_key_pem_base64(private_key_path)
    encrypted_number = encrypted_field(
        client.encrypted_value(card_id, "number", public_key),
        "encrypted_card_number",
    )
    return rsa_decrypt_text(encrypted_number, private_key_path)


def find_card_id_by_card_value(client: WallesterClient, card_value: str) -> str:
    value = card_value.strip()
    if CARD_ID_RE.match(value):
        return value

    card_number = card_number_digits(value)
    masked_number = masked_number_from_card_number(card_number)
    response = client.search_cards(
        {
            "masked_card_number": masked_number,
            "from_record": 0,
            "records_count": 10,
        }
    )
    cards = response.get("cards", []) if isinstance(response, dict) else []
    if not cards:
        raise ValueError(f"карта не найдена по маске {masked_number}")
    if len(cards) == 1:
        card_id = cards[0].get("id")
        if not card_id:
            raise ValueError(f"у найденной карты нет ID по маске {masked_number}")
        return str(card_id)

    for card in cards:
        card_id = card.get("id")
        if not card_id:
            continue
        try:
            if decrypted_card_number(client, str(card_id)) == card_number:
                return str(card_id)
        except (JWTError, WallesterError, ValueError):
            continue
    raise ValueError(f"найдено несколько карт по маске {masked_number}, но точное совпадение по полному номеру не найдено")


def rename_cards(client: WallesterClient) -> None:
    print("\nПереименование карт")
    rows = load_rename_rows(DEFAULT_RENAME_SHEET_URL)
    if not rows:
        print("В таблице нет строк, где заполнены Cards и Имя баера.")
        return

    print(f"\nНайдено строк для переименования: {len(rows)}")
    print("Первые строки:")
    for row in rows[:5]:
        print(f"- {row['card_value']} -> {row['name']}")
    if len(rows) > 5:
        print(f"... еще {len(rows) - 5}")

    if not ask_bool("Переименовать карты?", False):
        print("Операция отменена.")
        return

    renamed_cards = []
    errors = []
    for index, row in enumerate(rows, start=1):
        card_value = row["card_value"]
        new_name = row["name"]
        print(f"\nПереименование {index}/{len(rows)}: {card_value} -> {new_name}")
        try:
            card_id = find_card_id_by_card_value(client, card_value)
            client.rename_card(card_id, new_name)
        except (ValueError, WallesterError) as exc:
            errors.append(f"{card_value}: {exc}")
            print(f"Ошибка: {exc}")
            continue
        renamed_cards.append({"card_value": card_value, "name": new_name})
        print("Готово")

    print("\nКарты переименованы")
    print("=" * 20)
    for renamed_card in renamed_cards:
        print(f'{renamed_card["card_value"]} - новое имя "{renamed_card["name"]}"')
    if errors:
        print(f"\nОшибок: {len(errors)}")
        for error in errors:
            print(f"- {error}")


def close_cards(client: WallesterClient) -> None:
    print("\nЗакрытие карт")
    card_values = load_card_values(DEFAULT_CLOSE_SHEET_URL)
    if not card_values:
        print("В таблице нет строк, где заполнен столбец Cards.")
        return

    reason_label = CLOSE_REASON_OPTIONS[0]["label"]
    reason = CLOSE_REASON_OPTIONS[0]["value"]
    print(f"\nНайдено карт для закрытия: {len(card_values)}")
    print(f"Причина закрытия: {reason_label}")
    print("Карты:")
    for card_value in card_values:
        print(f"- {card_value}")

    if not ask_bool("Закрыть карты?", False):
        print("Операция отменена.")
        return

    success_count = 0
    errors = []
    for index, card_value in enumerate(card_values, start=1):
        print(f"\nЗакрытие {index}/{len(card_values)}: {card_value}")
        try:
            card_id = find_card_id_by_card_value(client, card_value)
            client.close_card(card_id, reason)
        except (ValueError, WallesterError) as exc:
            errors.append(f"{card_value}: {exc}")
            print(f"Ошибка: {exc}")
            continue
        success_count += 1
        print("Готово")

    print("\nКарты успешно закрыты")
    print(f"Успешно: {success_count}")
    if errors:
        print(f"\nОшибок: {len(errors)}")
        for error in errors:
            print(f"- {error}")


def show_menu() -> str:
    clear_screen()
    print("Терминальный клиент Wallester")
    print("=" * 31)
    print("1. Выпустить Expense Virtual Card")
    print("2. Переименовать карты")
    print("3. Закрыть карты")
    print("q. Выход")
    return input("Выберите действие: ").strip().lower()


def main() -> int:
    setup_terminal()
    client = build_client()
    actions = {
        "1": issue_card,
        "2": rename_cards,
        "3": close_cards,
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
