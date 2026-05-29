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
BASE_URL=https://api-frontend.wallester.com
TOKEN=replace_with_jwt_token
3DS_EMAIL=codes@example.com
3DS_PASSWORD=Card3DS2026!
```

Скрипт использует только стандартную библиотеку Python, зависимости устанавливать не нужно.

## Команды

Посмотреть список команд:

```powershell
python .\wallester_cards.py --help
```

Временный терминальный интерфейс:

```powershell
python .\terminal_clien.py
```

## Интерактивный терминальный режим

Если не хочется каждый раз вводить длинные команды с параметрами, используйте временный терминальный интерфейс:

```powershell
python .\terminal_clien.py
```

После запуска появится меню:

```text
Терминальный клиент Wallester
===============================
1. Выпустить Expense Virtual Card
2. Получить данные по карте
3. Выгрузить карты
4. Переименовать карту
5. Закрыть карту
q. Выход
```

Сценарий работы:

1. Заполните `.env`, чтобы интерфейс сам подхватил токен:

```env
BASE_URL=https://api-frontend.wallester.com
TOKEN=replace_with_jwt_token
3DS_EMAIL=codes@example.com
3DS_PASSWORD=Card3DS2026!
```

Терминальный интерфейс не спрашивает JWT вручную. Если `TOKEN` не заполнен в `.env`, запуск остановится с ошибкой.

2. Запустите интерфейс:

```powershell
python .\terminal_clien.py
```

3. Введите номер действия из меню. Для выхода из программы в главном меню введите `q`.

4. Отвечайте на вопросы в терминале. Если поле необязательное, его можно оставить пустым и нажать Enter. Для возврата из любого действия в главное меню введите `q`.

5. Для подтверждений используется `1` для ответа “да” и `0` для ответа “нет”.

6. После выполнения операции интерфейс покажет краткую сводку по карте и предложит сохранить полный JSON-ответ в файл.

### Выпуск карты через меню

Выберите `1. Выпустить карту`.

Интерфейс выпускает только `Expense Card` с типом `Virtual`. Поля `type`, `is_disposable`, `security` и 3DS-настройки подставляются автоматически. Пользователь выбирает счет и количество карт.

Названия генерируются автоматически:

```text
Auto card 001
Auto card 002
Auto card 003
```

`send_notification` всегда отправляется как `false`, webhook-уведомления при выпуске не запрашиваются.

Перед реальным выпуском интерфейс покажет итоговый payload и спросит подтверждение.

### Получение данных по карте

Выберите `2. Получить данные по карте`.

Нужно ввести `card_id`. Дополнительно можно получить зашифрованные значения:

- `number`
- `cvv2`
- `pin`
- `3ds`

Для этого понадобится файл с base64 RSA public key.

### Выгрузка карт

Выберите `3. Выгрузить список карт`.

Можно указать фильтры:

- `masked_card_number`
- `reference_number`
- `external_id`
- `from_record`
- `records_count`

После выгрузки интерфейс покажет первые найденные карты. Если указать путь сохранения с расширением `.csv`, результат будет сохранен в CSV. В остальных случаях сохранится JSON.

Примеры путей:

```text
.\cards.csv
.\cards.json
.\exports\cards.csv
```

### Переименование карты

Выберите `4. Переименовать карту`.

Нужно ввести:

- `card_id`
- новое название карты

После изменения можно сохранить полный ответ API в JSON-файл.

### Закрытие карты

Выберите `5. Закрыть карту`.

Интерфейс попросит:

- `card_id`
- причину закрытия
- ручное подтверждение словом `CLOSE`

Без ввода `CLOSE` карта не будет закрыта.

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
