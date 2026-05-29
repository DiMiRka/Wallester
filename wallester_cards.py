from __future__ import annotations

import argparse
import base64
import csv
import email.utils
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://api-frontend.wallester.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
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
PROJECT_ROOT = Path(__file__).resolve().parent


class WallesterError(RuntimeError):
    pass


class JWTError(RuntimeError):
    pass


class DERReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def read_byte(self) -> int:
        if self.pos >= len(self.data):
            raise JWTError("Unexpected end of DER data.")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def read_length(self) -> int:
        first = self.read_byte()
        if first < 0x80:
            return first
        size = first & 0x7F
        if size == 0:
            raise JWTError("Indefinite DER lengths are not supported.")
        value = 0
        for _ in range(size):
            value = (value << 8) | self.read_byte()
        return value

    def read_tlv(self, expected_tag: int | None = None) -> bytes:
        tag = self.read_byte()
        if expected_tag is not None and tag != expected_tag:
            raise JWTError(f"Unexpected DER tag 0x{tag:02x}; expected 0x{expected_tag:02x}.")
        length = self.read_length()
        end = self.pos + length
        if end > len(self.data):
            raise JWTError("DER length exceeds input size.")
        value = self.data[self.pos:end]
        self.pos = end
        return value

    def eof(self) -> bool:
        return self.pos >= len(self.data)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def read_pem_der(path: str) -> tuple[str, bytes]:
    text = Path(path).read_text(encoding="utf-8").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header = next((line for line in lines if line.startswith("-----BEGIN ")), "")
    footer = next((line for line in lines if line.startswith("-----END ")), "")
    if not header or not footer:
        raise JWTError(f"{path}: expected PEM private key.")
    label = header.removeprefix("-----BEGIN ").removesuffix("-----")
    if "ENCRYPTED" in label:
        raise JWTError("Encrypted private keys are not supported. Use an unencrypted PEM private key.")
    body = "".join(line for line in lines if not line.startswith("-----"))
    return label, base64.b64decode(body)


def parse_int(reader: DERReader) -> int:
    value = reader.read_tlv(0x02)
    return int.from_bytes(value, "big", signed=False)


def parse_pkcs1_private_key(der: bytes) -> tuple[int, int, int]:
    seq = DERReader(DERReader(der).read_tlv(0x30))
    _version = parse_int(seq)
    modulus = parse_int(seq)
    public_exponent = parse_int(seq)
    private_exponent = parse_int(seq)
    return modulus, public_exponent, private_exponent


def parse_pkcs8_private_key(der: bytes) -> tuple[int, int, int]:
    seq = DERReader(DERReader(der).read_tlv(0x30))
    _version = parse_int(seq)
    _algorithm_identifier = seq.read_tlv(0x30)
    private_key_octets = seq.read_tlv(0x04)
    return parse_pkcs1_private_key(private_key_octets)


def resolve_project_path(path: str) -> Path:
    private_key_path = Path(path).expanduser()
    if not private_key_path.is_absolute():
        private_key_path = PROJECT_ROOT / private_key_path
    return private_key_path


def load_rsa_private_numbers(path: str) -> tuple[int, int, int]:
    private_key_path = resolve_project_path(path)
    label, der = read_pem_der(str(private_key_path))
    if label == "RSA PRIVATE KEY":
        return parse_pkcs1_private_key(der)
    if label == "PRIVATE KEY":
        return parse_pkcs8_private_key(der)
    raise JWTError(f"Unsupported PEM key type: {label}")


def rsa_sha256_sign(message: bytes, modulus: int, private_exponent: int) -> bytes:
    digest = hashlib.sha256(message).digest()
    digest_info_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    digest_info = digest_info_prefix + digest
    key_size = (modulus.bit_length() + 7) // 8
    padding_size = key_size - len(digest_info) - 3
    if padding_size < 8:
        raise JWTError("RSA key is too small for RS256 signing.")
    encoded = b"\x00\x01" + (b"\xff" * padding_size) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus)
    return signature.to_bytes(key_size, "big")


def der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def der_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + der_len(len(value)) + value


def der_int(value: int) -> bytes:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return der_tlv(0x02, raw)


def rsa_public_key_pem_base64(private_key_path: str) -> str:
    modulus, public_exponent, _private_exponent = load_rsa_private_numbers(private_key_path)
    rsa_public_key = der_tlv(0x30, der_int(modulus) + der_int(public_exponent))
    algorithm = der_tlv(0x30, der_tlv(0x06, bytes.fromhex("2a864886f70d010101")) + der_tlv(0x05, b""))
    subject_public_key_info = der_tlv(0x30, algorithm + der_tlv(0x03, b"\x00" + rsa_public_key))
    body = base64.encodebytes(subject_public_key_info).decode("ascii").replace("\n", "")
    lines = [body[index : index + 64] for index in range(0, len(body), 64)]
    pem = "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"
    return base64.b64encode(pem.encode("ascii")).decode("ascii")


def mgf1(seed: bytes, length: int, hash_name: str) -> bytes:
    output = b""
    counter = 0
    while len(output) < length:
        output += hashlib.new(hash_name, seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return output[:length]


def xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def unpad_pkcs1_v15(encoded: bytes) -> bytes | None:
    if not encoded.startswith(b"\x00\x02"):
        return None
    separator = encoded.find(b"\x00", 2)
    if separator < 10:
        return None
    return encoded[separator + 1 :]


def unpad_oaep(
    encoded: bytes,
    hash_name: str,
    mgf_hash_name: str | None = None,
    label: bytes = b"",
) -> bytes | None:
    mgf_hash_name = mgf_hash_name or hash_name
    digest_size = hashlib.new(hash_name).digest_size
    if len(encoded) < 2 * digest_size + 2 or encoded[0] != 0:
        return None
    masked_seed = encoded[1 : 1 + digest_size]
    masked_db = encoded[1 + digest_size :]
    seed = xor_bytes(masked_seed, mgf1(masked_db, digest_size, mgf_hash_name))
    data_block = xor_bytes(masked_db, mgf1(seed, len(masked_db), mgf_hash_name))
    label_hash = hashlib.new(hash_name, label).digest()
    if data_block[:digest_size] != label_hash:
        return None
    index = digest_size
    while index < len(data_block) and data_block[index] == 0:
        index += 1
    if index >= len(data_block) or data_block[index] != 1:
        return None
    return data_block[index + 1 :]


def clean_encrypted_message(encrypted_value: str) -> tuple[str, bytes | None]:
    labels = []
    body_lines = []
    for raw_line in str(encrypted_value).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("-----BEGIN ") and line.endswith(" MESSAGE-----"):
            label = line.removeprefix("-----BEGIN ").removesuffix(" MESSAGE-----")
            labels.append(label.encode("utf-8"))
            continue
        if line.startswith("-----END ") and line.endswith(" MESSAGE-----"):
            continue
        body_lines.append(line)
    return "".join(body_lines), labels[0] if labels else None


def rsa_decrypt_text(encrypted_value: str, private_key_path: str) -> str:
    modulus, _public_exponent, private_exponent = load_rsa_private_numbers(private_key_path)
    encrypted_compact, message_label = clean_encrypted_message(encrypted_value)
    encrypted_compact += "=" * (-len(encrypted_compact) % 4)
    cipher = base64.urlsafe_b64decode(encrypted_compact)
    key_size = (modulus.bit_length() + 7) // 8
    encoded = pow(int.from_bytes(cipher, "big"), private_exponent, modulus).to_bytes(key_size, "big")
    oaep_variants = [
        ("sha1", "sha1"),
        ("sha224", "sha224"),
        ("sha256", "sha256"),
        ("sha384", "sha384"),
        ("sha512", "sha512"),
        ("sha256", "sha1"),
        ("sha384", "sha1"),
        ("sha512", "sha1"),
    ]
    labels = [b""]
    if message_label:
        labels.insert(0, message_label)
    for unpadded in (
        unpad_pkcs1_v15(encoded),
        *(
            unpad_oaep(encoded, hash_name, mgf_hash_name, label)
            for label in labels
            for hash_name, mgf_hash_name in oaep_variants
        ),
    ):
        if unpadded is not None:
            return unpadded.decode("utf-8")
    raise JWTError("Unable to decrypt RSA value with supported paddings.")


class WallesterJWTProvider:
    def __init__(self, api_key: str, private_key_path: str, timestamp_offset: int = 0) -> None:
        if not api_key:
            raise JWTError("API_KEY is empty.")
        if not private_key_path:
            raise JWTError("PRIVATE_KEY_PATH is empty.")
        self.api_key = api_key
        self.timestamp_offset = timestamp_offset
        self.modulus, _public_exponent, self.private_exponent = load_rsa_private_numbers(private_key_path)

    def adjust_timestamp_offset(self, server_date: str | None = None) -> None:
        if server_date:
            try:
                server_time = email.utils.parsedate_to_datetime(server_date).timestamp()
            except (TypeError, ValueError):
                server_time = None
            if server_time is not None:
                self.timestamp_offset = math.ceil(server_time - time.time())
                return
        self.timestamp_offset = 0

    def token(self) -> str:
        header = {"typ": "JWT", "alg": "RS256"}
        payload = {"api_key": self.api_key, "ts": int(time.time()) + self.timestamp_offset}
        signing_input = (
            b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
            + "."
            + b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        ).encode("ascii")
        signature = rsa_sha256_sign(signing_input, self.modulus, self.private_exponent)
        return signing_input.decode("ascii") + "." + b64url(signature)


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
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        timeout: int = 60,
        jwt_provider: WallesterJWTProvider | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token
        self.jwt_provider = jwt_provider
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": user_agent or DEFAULT_USER_AGENT,
        }

    def auth_headers(self) -> dict[str, str]:
        token = self.jwt_provider.token() if self.jwt_provider else self.token
        if not token:
            raise WallesterError("JWT token is not configured.")
        return {**self.headers, "Authorization": f"Bearer {token}"}

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

        payload = self._urlopen_json(method, path, url, data)

        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))

    def _urlopen_json(self, method: str, path: str, url: str, data: bytes | None) -> bytes:
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method=method, headers=self.auth_headers())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                if exc.code == 401 and "JWT token expired" in details and attempt < 2:
                    if self.jwt_provider:
                        self.jwt_provider.adjust_timestamp_offset(exc.headers.get("Date"))
                    continue
                raise WallesterError(f"{method} {path} failed: HTTP {exc.code}: {details}") from exc
            except urllib.error.URLError as exc:
                raise WallesterError(f"{method} {path} failed: {exc.reason}") from exc
        return b""

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

    def search_accounts(self, query: dict[str, Any]) -> Any:
        return self.request("GET", "/v1/accounts", query=query)

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
    token = args.token or os.getenv("TOKEN")
    api_key = os.getenv("API_KEY")
    private_key_path = os.getenv("PRIVATE_KEY_PATH")
    jwt_provider = None
    if not token:
        if not api_key or not private_key_path:
            raise SystemExit("Set API_KEY and PRIVATE_KEY_PATH in .env, or pass --token.")
        jwt_provider = WallesterJWTProvider(
            api_key=api_key,
            private_key_path=private_key_path,
        )
    base_url = args.base_url or os.getenv("BASE_URL", DEFAULT_BASE_URL)
    user_agent = os.getenv("USER_AGENT", DEFAULT_USER_AGENT)
    return WallesterClient(
        base_url=base_url,
        token=token,
        timeout=args.timeout,
        jwt_provider=jwt_provider,
        user_agent=user_agent,
    )


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
    parser.add_argument("--token", help="Manual JWT token for temporary testing. Normal mode uses API_KEY and PRIVATE_KEY_PATH.")
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

    export = subparsers.add_parser("export", help="Найти/выгрузить карты")
    export.add_argument("--masked-card-number")
    export.add_argument("--reference-number")
    export.add_argument("--external-id")
    export.add_argument("--from-record", type=int, default=0)
    export.add_argument("--records-count", type=int, default=100)
    export.add_argument("--order-direction", choices=["asc", "desc", "ASC", "DESC"])
    export.add_argument("--order-fields", action="append", help="Поле сортировки. Можно передавать несколько раз")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--fields", help="CSV-поля через запятую")
    export.add_argument("--full", action="store_true", help="Для JSON: вывести полный ответ с total_records_number")
    export.add_argument("--output", help="Записать выгрузку в файл")
    export.set_defaults(func=command_export)

    rename = subparsers.add_parser("rename", help="Переименовать карту")
    rename.add_argument("card_id")
    rename.add_argument("name")
    rename.add_argument("--output", help="Записать JSON-ответ в файл")
    rename.set_defaults(func=command_rename)

    close = subparsers.add_parser("close", help="Закрыть карту")
    close.add_argument("card_id")
    close.add_argument("--reason", default="ClosedByClient", choices=[
        "ClosedByIssuer",
        "ClosedByClient",
        "ClosedBySystem",
        "ClosedByCardholder",
        "ClosedByReplace",
    ])
    close.add_argument("--yes", action="store_true", help="Подтвердить операцию, которая меняет состояние карты")
    close.add_argument("--output", help="Записать JSON-ответ в файл")
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
