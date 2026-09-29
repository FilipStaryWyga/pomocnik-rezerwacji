# Pomocnik rezerwacji

Program czuwa nad skrzynką pocztową i reaguje na odrzucenia zapytań o zwiedzanie grupowe
w Państwowym Muzeum Auschwitz-Birkenau. Gdy przyjdzie odrzucenie:

1. wysyła alarm na telefon (aplikacja ntfy),
2. gra alarm i pokazuje powiadomienie na komputerze,
3. otwiera formularz w Chrome, wypełniony tym samym terminem.

CAPTCHA („Nie jestem robotem”) i przycisk „Wyślij” zawsze zostają dla człowieka.

## Jak części programu współpracują

- **Strona ustawień** (http://127.0.0.1:47631) – stan czuwania, lista wiadomości z muzeum,
  przycisk „Otwórz formularz rezerwacji” i „Wypełnij ponownie” przy każdym odrzuceniu
  (otwiera Chrome z formularzem wypełnionym tym terminem). Kafelki stanu prowadzą do właściwych sekcji;
  kafelek „Skrypt w Chrome” pokazuje, gdy skrypt jest nieaktualny.
- **Panel w Chrome** (na visit.auschwitz.org) – szablony i dziennik zgłoszeń. Odrzucenia z poczty same
  trafiają do dziennika, a potwierdzenia z terminem w temacie można jednym kliknięciem oznaczyć jako
  przydzielone. Karta otwarta wcześniej pokazuje baner „Wypełnij teraz” zamiast nadpisywać formularz.
  Ikona koła zębatego otwiera stronę ustawień. Zmiany w jednej karcie od razu widać w pozostałych.
- **Telefon** (ntfy) – alarm o odrzuceniu z najwyższym priorytetem; próbne alarmy mają dopisek „PRÓBA”.

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
   Sprawdź zmiany testami: `python3 test_windows.py` (działa też na Macu).
2. Wypchnij zmiany i tag, np. `git tag v4.1 && git push --tags`.
3. GitHub zbuduje i przetestuje plik. Link do pobrania zawsze wskazuje najnowszą wersję:
   `https://github.com/<konto>/<repozytorium>/releases/latest/download/PomocnikRezerwacji.exe`

Klient uruchamia pobrany plik – program instaluje się sam, a przy nowszej wersji aktualizuje
(ustawienia i hasło zostają).

Program nigdy sam nie pobiera ani nie instaluje nowych wersji. Co kilka godzin sprawdza tylko na GitHubie,
czy jest nowsze wydanie, i wtedy raz powiadamia (telefon + komputer). Na stronie ustawień pojawia się
przycisk „Pobierz” z linkiem powyżej. Powiadomienia można wyłączyć w sekcji 5 strony ustawień.
