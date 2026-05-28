from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://api-frontend.wallester.com"
DEFAULT_EXPORT_FIELDS = [
    "id",
    "name",
    "status",
    "type",
    "masked_card_number",
    "account_id",
    "person_id",
    "company_id",
    "currency_code",
    "expiry_date",
    "created_at",
    "updated_at",
    "external_id",
    "reference_number",
]


class WallesterError(RuntimeError):
    pass


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def read_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: ожидался JSON-объект")
    return data


def compact_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def set_nested(target: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    current = target
    parts = key.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def output_json(data: Any, output: str | None = None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
        return
    print(text)


class WallesterClient:
    def __init__(self, base_url: str, token: str, timeout: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        query = {key: value for key, value in (query or {}).items() if value is not None}
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise WallesterError(f"{method} {path} failed: HTTP {exc.code}: {details}") from exc
        except urllib.error.URLError as exc:
            raise WallesterError(f"{method} {path} failed: {exc.reason}") from exc

        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))

    def create_card(self, payload: dict[str, Any], send_notification: bool | None = None) -> Any:
        return self.request(
            "POST",
            "/v1/cards",
            query={"send_notification": send_notification},
            body=payload,
        )

    def get_card(self, card_id: str) -> Any:
        return self.request("GET", f"/v1/cards/{urllib.parse.quote(card_id)}")

    def search_cards(self, query: dict[str, Any]) -> Any:
        return self.request("GET", "/v1/cards", query=query)

    def rename_card(self, card_id: str, name: str) -> Any:
        return self.request("PATCH", f"/v1/cards/{urllib.parse.quote(card_id)}/name", body={"name": name})

    def close_card(self, card_id: str, close_reason: str) -> Any:
        return self.request(
            "PATCH",
            f"/v1/cards/{urllib.parse.quote(card_id)}/close",
            body={"close_reason": close_reason},
        )

    def encrypted_value(self, card_id: str, kind: str, public_key: str) -> Any:
        paths = {
            "number": "encrypted-card-number",
            "cvv2": "encrypted-cvv2",
            "pin": "encrypted-pin",
            "3ds": "encrypted-3ds-password",
        }
        return self.request(
            "POST",
            f"/v1/cards/{urllib.parse.quote(card_id)}/{paths[kind]}",
            body={"public_key": public_key},
        )


def build_client(args: argparse.Namespace) -> WallesterClient:
    load_env_file(Path(args.env_file))
    token = args.token or os.getenv("WALLESTER_TOKEN")
    if not token:
        raise SystemExit("Укажите WALLESTER_TOKEN в .env/переменных окружения или передайте --token.")
    base_url = args.base_url or os.getenv("WALLESTER_BASE_URL", DEFAULT_BASE_URL)
    return WallesterClient(base_url=base_url, token=token, timeout=args.timeout)


def command_issue(args: argparse.Namespace) -> None:
    client = build_client(args)
    payload = read_json(args.payload)

    values = compact_dict(
        {
            "type": args.card_type,
            "account_id": args.account_id,
            "name": args.name,
            "embossing_name": args.embossing_name,
            "embossing_company_name": args.embossing_company_name,
            "expiration_date": args.expiration_date,
            "is_disposable": args.is_disposable,
            "personalization_product_code": args.personalization_product_code,
        }
    )
    payload.update(values)

    for key, value in {
        "security.contactless_enabled": args.contactless_enabled,
        "security.internet_purchase_enabled": args.internet_purchase_enabled,
        "security.withdrawal_enabled": args.withdrawal_enabled,
        "security.all_time_limits_enabled": args.all_time_limits_enabled,
        "3d_secure_settings.mobile": args.secure_mobile,
        "3d_secure_settings.email": args.secure_email,
        "3d_secure_settings.password": args.secure_password,
        "3d_secure_settings.out_of_band_enabled": args.out_of_band_enabled,
        "3d_secure_settings.out_of_band_id": args.out_of_band_id,
    }.items():
        set_nested(payload, key, value)

    if not payload.get("type"):
        raise SystemExit("Нужен тип карты: передайте --type Virtual|ChipAndPin или используйте --payload.")

    result = client.create_card(payload, send_notification=args.send_notification)
    output_json(result, args.output)


def command_get(args: argparse.Namespace) -> None:
    client = build_client(args)
    result = client.get_card(args.card_id)
    if args.public_key_file:
        public_key = Path(args.public_key_file).read_text(encoding="utf-8").strip()
        result["encrypted"] = {
            kind: client.encrypted_value(args.card_id, kind, public_key)
            for kind in args.encrypted
        }
    output_json(result, args.output)


def command_export(args: argparse.Namespace) -> None:
    client = build_client(args)
    query = compact_dict(
        {
            "masked_card_number": args.masked_card_number,
            "reference_number": args.reference_number,
            "external_id": args.external_id,
            "from_record": args.from_record,
            "records_count": args.records_count,
            "order_direction": args.order_direction,
            "order_fields": args.order_fields,
        }
    )
    result = client.search_cards(query)
    cards = result.get("cards", [])

    if args.format == "json":
        output_json(result if args.full else cards, args.output)
        return

    fields = args.fields.split(",") if args.fields else DEFAULT_EXPORT_FIELDS
    if not args.output:
        writer = csv.DictWriter(sys.stdout, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(cards)
        return

    with open(args.output, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(cards)


def command_rename(args: argparse.Namespace) -> None:
    client = build_client(args)
    output_json(client.rename_card(args.card_id, args.name), args.output)


def command_close(args: argparse.Namespace) -> None:
    if not args.yes:
        raise SystemExit("Закрытие карты меняет ее состояние. Повторите команду с --yes для подтверждения.")
    client = build_client(args)
    output_json(client.close_card(args.card_id, args.reason), args.output)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", default=".env", help="Путь к dotenv-конфигу.")
    parser.add_argument("--base-url", help=f"Базовый URL API. По умолчанию: {DEFAULT_BASE_URL}")
    parser.add_argument("--token", help="JWT-токен. Лучше хранить в WALLESTER_TOKEN внутри .env.")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout в секундах.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Автоматизация выпуска, выгрузки, переименования и закрытия карт Wallester."
    )
    add_common_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    issue = subparsers.add_parser("issue", help="Выпустить карту.")
    issue.add_argument("--payload", help="JSON-тело запроса для POST /v1/cards.")
    issue.add_argument("--type", dest="card_type", choices=["Virtual", "ChipAndPin"], help="Тип карты.")
    issue.add_argument("--account-id", help="ID счета для карты.")
    issue.add_argument("--name", help="Название карты.")
    issue.add_argument("--embossing-name")
    issue.add_argument("--embossing-company-name")
    issue.add_argument("--expiration-date", help="Дата в формате YYYY-MM-DD.")
    issue.add_argument("--is-disposable", action=argparse.BooleanOptionalAction, default=None)
    issue.add_argument("--personalization-product-code")
    issue.add_argument("--contactless-enabled", action=argparse.BooleanOptionalAction, default=None)
    issue.add_argument("--internet-purchase-enabled", action=argparse.BooleanOptionalAction, default=None)
    issue.add_argument("--withdrawal-enabled", action=argparse.BooleanOptionalAction, default=None)
    issue.add_argument("--all-time-limits-enabled", action=argparse.BooleanOptionalAction, default=None)
    issue.add_argument("--secure-mobile", help="Телефон для 3DS.")
    issue.add_argument("--secure-email", help="Email для 3DS.")
    issue.add_argument("--secure-password", help="Пароль 3DS.")
    issue.add_argument("--out-of-band-enabled", action=argparse.BooleanOptionalAction, default=None)
    issue.add_argument("--out-of-band-id")
    issue.add_argument("--send-notification", action=argparse.BooleanOptionalAction, default=None)
    issue.add_argument("--output", help="Записать JSON-ответ в файл.")
    issue.set_defaults(func=command_issue)

    get = subparsers.add_parser("get", help="Получить карту по ID.")
    get.add_argument("card_id")
    get.add_argument("--public-key-file", help="Base64 RSA public key для получения зашифрованных данных карты.")
    get.add_argument("--encrypted", choices=["number", "cvv2", "pin", "3ds"], nargs="+", default=["number", "cvv2"])
    get.add_argument("--output", help="Записать JSON-ответ в файл.")
    get.set_defaults(func=command_get)

    export = subparsers.add_parser("export", help="Найти/выгрузить карты.")
    export.add_argument("--masked-card-number")
    export.add_argument("--reference-number")
    export.add_argument("--external-id")
    export.add_argument("--from-record", type=int, default=0)
    export.add_argument("--records-count", type=int, default=100)
    export.add_argument("--order-direction", choices=["asc", "desc", "ASC", "DESC"])
    export.add_argument("--order-fields", action="append", help="Поле сортировки. Можно передавать несколько раз.")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--fields", help="CSV-поля через запятую.")
    export.add_argument("--full", action="store_true", help="Для JSON: вывести полный ответ с total_records_number.")
    export.add_argument("--output", help="Записать выгрузку в файл.")
    export.set_defaults(func=command_export)

    rename = subparsers.add_parser("rename", help="Переименовать карту.")
    rename.add_argument("card_id")
    rename.add_argument("name")
    rename.add_argument("--output", help="Записать JSON-ответ в файл.")
    rename.set_defaults(func=command_rename)

    close = subparsers.add_parser("close", help="Закрыть карту.")
    close.add_argument("card_id")
    close.add_argument("--reason", default="ClosedByClient", choices=[
        "ClosedByIssuer",
        "ClosedByClient",
        "ClosedBySystem",
        "ClosedByCardholder",
        "ClosedByReplace",
    ])
    close.add_argument("--yes", action="store_true", help="Подтвердить операцию, которая меняет состояние карты.")
    close.add_argument("--output", help="Записать JSON-ответ в файл.")
    close.set_defaults(func=command_close)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except WallesterError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
