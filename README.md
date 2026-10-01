# hz-bot — bot do automatycznej gry w Hero Zero

Bot w Pythonie, który sam gra w [Hero Zero](https://www.herozerogame.com): wybiera i kończy
misje, toczy pojedynki, chodzi do pracy, odbiera nagrody. Komunikuje się bezpośrednio z API gry
(`request.php`) — nie klika w przeglądarce, więc działa szybko i może chodzić na serwerze 24/7.

> ⚠️ **Uwaga:** korzystanie z botów jest niezgodne z regulaminem Hero Zero i może skończyć się
> blokadą konta. Używasz na własne ryzyko — najlepiej najpierw na koncie testowym.

## Jak to działa

Klient gry wysyła żądania `POST https://<serwer>.herozerogame.com/request.php` z polami
`action`, `user_id`, `user_session_id`, `client_version`, … oraz podpisem
`auth = md5(action + sól + user_id)`. Serwer odpowiada JSON-em `{"data": {...}, "error": ""}`.

Sól i dokładne parametry są zaszyte w oficjalnym kliencie i mogą zmieniać się z wersjami gry,
dlatego bot **nie ma ich na sztywno**. Zamiast tego komenda `capture`:

1. otwiera grę w prawdziwej przeglądarce (Playwright), w której logujesz się normalnie,
2. przechwytuje żądania wysyłane przez grę i pobrane skrypty JS,
3. sama znajduje sól (sprawdza, który ciąg znaków z kodu gry odtwarza podpisy `auth`),
4. zapisuje sesję, stałe parametry (np. `client_version`), szablon logowania (bez hasła!)
   i listę zaobserwowanych akcji do `session.json`.

## Instalacja

Wymagany Python 3.10+.

```bash
git clone <repo> hz-bot && cd hz-bot
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[capture]"
playwright install chromium
cp config.example.yaml config.yaml                   # i ustaw swój serwer (np. pl1)
```

## Użycie

```bash
# 1. Zaloguj się w oknie przeglądarki i zagraj chwilę: rozpocznij i odbierz misję,
#    stocz pojedynek, ewentualnie pracę. Potem naciśnij Enter w terminalu.
python -m hzbot capture --server pl1

# 2. Sprawdź, czy nazwy akcji z config.yaml zgadzają się z tym, co wysyła gra.
python -m hzbot doctor --ping

# 3. Najpierw na sucho - bot tylko loguje, co by zrobił.
python -m hzbot run --dry-run

# 4. Gramy!
python -m hzbot run
```

Pozostałe komendy:

| Komenda | Opis |
| --- | --- |
| `simulate --hours 24` | uruchamia bota na wbudowanym symulatorze gry (bez łączenia z Hero Zero) — dobre do testów strategii |
| `call AKCJA k=v …` | wysyła pojedynczą akcję i wypisuje odpowiedź (debugowanie) |
| `login` | loguje ponownie e-mailem/hasłem z konfiguracji (`HZ_EMAIL`, `HZ_PASSWORD`) |

Jeśli `doctor` pokazuje `??` przy jakiejś akcji, wykonaj ją ręcznie w grze podczas kolejnego
`capture` (wyniki z wielu przechwyceń są łączone) albo popraw nazwę w sekcji `actions:` w
`config.yaml` — `doctor` wypisuje też wszystkie inne akcje zaobserwowane w ruchu gry.

## Co robi bot

W pętli, z losowymi przerwami między akcjami:

1. **Misja w toku** → czeka do jej końca, sprawdza ukończenie i odbiera nagrodę.
2. **Praca w toku** → czeka do końca i odbiera wypłatę.
3. **Pojedynki** → gdy jest kondycja i nie przekroczono dziennego limitu: pobiera listę rywali,
   wybiera wg strategii (`weakest` lub `honor`), walczy i odbiera nagrodę. Nie walczy dwa razy
   tego samego dnia z tym samym rywalem.
4. **Nowa misja** → wybiera najlepszą dostępną wg strategii (`xp_per_energy`,
   `coins_per_energy`, `xp_per_minute`, `coins_per_minute`, `balanced`), z uwzględnieniem
   rezerwy energii, maks. czasu trwania, pomijania walk i preferowania przedmiotów.
5. **Praca** (opcjonalnie) → gdy nie ma nic innego do zrobienia.
6. Inaczej czeka `idle_poll_minutes` i sprawdza stan ponownie.

Dodatkowo: godziny aktywności (`active_hours`), limit czasu działania, automatyczne ponowne
logowanie po wygaśnięciu sesji, ponawianie przy błędach sieci i zatrzymanie po serii błędów.
Po zakończeniu (także Ctrl+C) wypisuje podsumowanie. Wszystkie opcje opisuje
[`config.example.yaml`](config.example.yaml).

## Struktura

```
hzbot/
  auth.py      podpis md5 i wykrywanie soli z kodu JS
  capture.py   przechwytywanie protokołu z przeglądarki (Playwright)
  client.py    klient request.php (podpisy, ponawianie, błędy, dry-run)
  session.py   plik sesji
  state.py     lokalny stan gry składany z odpowiedzi serwera
  strategy.py  wybór misji i przeciwników
  bot.py       główna pętla
  sim.py       symulator serwera gry (testy, `simulate`)
  config.py    konfiguracja YAML
  cli.py       komendy
tests/         pytest (w tym pełna doba gry na symulatorze)
```

## Testy

```bash
pip install -e ".[dev]"
pytest
```

## Ograniczenia

- Nazwy akcji i pól odpowiedzi pochodzą z publicznie dostępnych informacji o protokole i nie
  były testowane na żywym serwerze — po aktualizacji gry mogą wymagać poprawek w `actions:`.
  Bot czyta pola odpowiedzi defensywnie (np. `quest_energy`, `duel_stamina`,
  `active_quest_id`, `ts_complete`), a nieznane pola ignoruje.
- Jeśli gra zacznie wysyłać parametr zmieniający się z każdym żądaniem, `capture` wypisze
  ostrzeżenie — taki parametr trzeba wtedy obsłużyć w kodzie.
- `session.json` zawiera aktywną sesję konta — nie udostępniaj go (jest w `.gitignore`).
