# Pomocnik rezerwacji

Program czuwa nad skrzynką pocztową i reaguje na odrzucenia zapytań o zwiedzanie grupowe
w Państwowym Muzeum Auschwitz-Birkenau. Gdy przyjdzie odrzucenie:

1. wysyła alarm na telefon (aplikacja ntfy),
2. gra alarm i pokazuje powiadomienie na komputerze,
3. otwiera formularz w Chrome, wypełniony tym samym terminem.

CAPTCHA („Nie jestem robotem”) i przycisk „Wyślij” zawsze zostają dla człowieka.

## Pliki

| Plik | Do czego |
|---|---|
| `pomocnik.py` | program (czuwanie nad pocztą, alarmy, strona ustawień pod http://127.0.0.1:47631) |
| `pomocnik.user.js` | skrypt Tampermonkey do Chrome: szablony, dziennik zgłoszeń, wypełnianie formularza |
| `ikona.py` | generuje ikonę programu |
| `zbuduj_mac.sh` | buduje aplikację na Maca (`dist/Pomocnik rezerwacji.app`) |
| `test_windows.py` | testy uruchamiane przed zbudowaniem wersji na Windows |
| `.github/workflows/windows.yml` | automatyczne budowanie i test `PomocnikRezerwacji.exe` |

## Nowa wersja na Windows

1. Zmień `WERSJA` w `pomocnik.py` (i `@version` w `pomocnik.user.js`, jeśli zmienił się skrypt).
2. Wypchnij zmiany i tag, np. `git tag v4.1 && git push --tags`.
3. GitHub zbuduje i przetestuje plik. Link do pobrania zawsze wskazuje najnowszą wersję:
   `https://github.com/<konto>/<repozytorium>/releases/latest/download/PomocnikRezerwacji.exe`

Klient uruchamia pobrany plik – program instaluje się sam, a przy nowszej wersji aktualizuje.
