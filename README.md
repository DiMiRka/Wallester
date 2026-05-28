# Автоматизация карт Wallester

Небольшой Python CLI-скрипт для базового жизненного цикла карт через Wallester Frontend API:

- выпуск карты: `POST /v1/cards`
- поиск и выгрузка карт: `GET /v1/cards`
- получение данных по карте: `GET /v1/cards/{card_id}`
- переименование карты: `PATCH /v1/cards/{card_id}/name`
- закрытие карты: `PATCH /v1/cards/{card_id}/close`

## Настройка

Скопируйте пример конфига и укажите JWT-токен:

```powershell
Copy-Item .env.example .env
notepad .env
```

В `.env` должны быть значения:

```env
WALLESTER_BASE_URL=https://api-frontend.wallester.com
WALLESTER_TOKEN=replace_with_jwt_token
```

Скрипт использует только стандартную библиотеку Python, зависимости устанавливать не нужно.

## Команды

Посмотреть список команд:

```powershell
python .\wallester_cards.py --help
```

Посмотреть параметры конкретной команды:

```powershell
python .\wallester_cards.py issue --help
python .\wallester_cards.py export --help
python .\wallester_cards.py close --help
```

## Выпуск карты

Минимальный пример выпуска виртуальной карты:

```powershell
python .\wallester_cards.py issue --type Virtual --account-id "<account_uuid>" --name "Travel card"
```

Выпуск карты из полного JSON-payload:

```powershell
python .\wallester_cards.py issue --payload .\card_payload.json --output .\issued_card.json
```

Минимальный payload по документации:

```json
{
  "type": "Virtual"
}
```

Часто используемые поля payload:

- `type`: `Virtual` или `ChipAndPin`
- `account_id`: ID счета
- `name`: название карты
- `embossing_name`: имя для эмбоссинга
- `expiration_date`: дата истечения в формате `YYYY-MM-DD`
- `is_disposable`: одноразовая карта, только для `Virtual`
- `personalization_product_code`: код дизайна/персонализации
- `security`: настройки безопасности
- `3d_secure_settings`: настройки 3DS
- `limits`: лимиты карты
- `delivery_address`: адрес доставки для физической карты

## Получение данных по карте

```powershell
python .\wallester_cards.py get "<card_uuid>" --output .\card.json
```

Получить зашифрованные PAN/CVV2 через RSA public key:

```powershell
python .\wallester_cards.py get "<card_uuid>" --public-key-file .\public_key.txt --encrypted number cvv2
```

Доступные значения для `--encrypted`:

- `number`: номер карты
- `cvv2`: CVV2
- `pin`: PIN
- `3ds`: 3DS password

## Выгрузка карт

Выгрузить карты в CSV:

```powershell
python .\wallester_cards.py export --records-count 500 --output .\cards.csv
```

Выгрузить в JSON:

```powershell
python .\wallester_cards.py export --format json --records-count 500 --output .\cards.json
```

Фильтры:

```powershell
python .\wallester_cards.py export --external-id "<external_id>" --output .\cards.csv
python .\wallester_cards.py export --reference-number "<reference_number>" --output .\cards.csv
python .\wallester_cards.py export --masked-card-number "416548******0998" --output .\cards.csv
```

Можно задать поля CSV:

```powershell
python .\wallester_cards.py export --fields id,name,status,type,masked_card_number,account_id --output .\cards.csv
```

## Переименование карты

```powershell
python .\wallester_cards.py rename "<card_uuid>" "New card name"
```

## Закрытие карты

Закрытие карты считается опасной операцией, поэтому нужен явный флаг `--yes`:

```powershell
python .\wallester_cards.py close "<card_uuid>" --reason ClosedByClient --yes
```

Доступные причины закрытия:

- `ClosedByIssuer`
- `ClosedByClient`
- `ClosedBySystem`
- `ClosedByCardholder`
- `ClosedByReplace`

## Важные замечания

- JWT-токен передается в заголовке `Authorization: Bearer <token>`
- Не храните реальный `.env` в git, он добавлен в `.gitignore`
- Реальные вызовы `issue` и `close` меняют состояние карт. Сначала проверяйте команды на тестовом окружении или тестовом аккаунте
