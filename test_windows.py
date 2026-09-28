"""
Testy części programu zależnych od systemu – uruchamiane automatycznie na Windowsie (GitHub Actions)
przed zbudowaniem pliku .exe. Działa też na Macu:  python3 test_windows.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

# Osobny katalog danych, żeby test nie ruszał prawdziwych ustawień.
tmp = tempfile.mkdtemp()
os.environ['APPDATA'] = tmp
os.environ['XDG_CONFIG_HOME'] = tmp
os.environ['POMOCNIK_PORT'] = '47699'
if sys.platform == 'darwin':
    os.environ['HOME'] = tmp  # katalog danych w ~/Library/Application Support

import pomocnik as p  # noqa: E402

bledy = []


def sprawdz(nazwa, warunek, szczegoly=''):
    print(('OK   ' if warunek else 'BŁĄD ') + nazwa + (f' – {szczegoly}' if szczegoly and not warunek else ''))
    if not warunek:
        bledy.append(nazwa)


print('System:', p.SYSTEM, '| dane:', p.DANE)

# Rozpoznawanie maila z odrzuceniem
from email.message import EmailMessage  # noqa: E402
m = EmailMessage()
m['From'] = 'Zwiedzanie Miejsca Pamięci Auschwitz <visit@museum.auschwitz.org>'
m['Subject'] = 'Odrzucenie zapytania o grupę - 2026-12-08 12:00 - angielski'
m.set_content('Zwiedzanie grupowe\n2026-12-08 12:00\nZwiedzanie ogólne 3,5 godz.\nangielski\n')
zd = p.rozpoznaj(m.as_bytes(), 'museum.auschwitz.org')
sprawdz('rozpoznanie odrzucenia', zd and zd['typ'] == 'odrzucenie' and zd['data'] == '2026-12-08'
        and zd['godzina'] == '12:00' and zd['rodzaj'] == 'Zwiedzanie ogólne 3,5 godz.', zd)

# Hasło w zabezpieczonym magazynie (Windows: DPAPI) – tylko na Windowsie, żeby nie ruszać Pęku kluczy
if p.SYSTEM == 'Windows':
    trudne = 'zażółć gęślą jaźń "\'$&<>'
    p.haslo_zapisz('test@example.com', trudne)
    sprawdz('zapis i odczyt hasła', p.haslo_odczytaj('test@example.com') == trudne)
    with open(p.PLIK_HASEL, 'rb') as f:
        sprawdz('hasło zaszyfrowane na dysku', trudne.encode() not in f.read())

    p.autostart_ustaw(True)
    sprawdz('autostart włączony', p.autostart_wlaczony())
    p.autostart_ustaw(False)
    sprawdz('autostart wyłączony', not p.autostart_wlaczony())

sprawdz('dźwięki systemowe', len(p.dostepne_dzwieki()) > 0, p.dostepne_dzwieki())
p.powiadom_system('Test', 'Test powiadomienia')
p.zagraj_alarm(p.konfig(), 1)

# Lokalny serwer
serwer = p.ThreadingHTTPServer(('127.0.0.1', p.PORT), p.Obsluga)
threading.Thread(target=serwer.serve_forever, daemon=True).start()
time.sleep(0.5)
baza = f'http://127.0.0.1:{p.PORT}'


def get(sciezka):
    with urllib.request.urlopen(baza + sciezka, timeout=5) as r:
        return r.status, r.read()


def post(sciezka, dane):
    req = urllib.request.Request(baza + sciezka, data=json.dumps(dane).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


kod, tresc = get('/')
sprawdz('strona ustawień', kod == 200 and b'Pomocnik rezerwacji' in tresc)
kod, tresc = get('/pomocnik.user.js')
sprawdz('skrypt dla Chrome', kod == 200 and b'==UserScript==' in tresc)
sprawdz('zapis ustawień', post('/api/ustawienia', {'glosnosc': 55})['ok'] and p.konfig()['glosnosc'] == 55)
stan = json.loads(get('/api/stan')[1])
sprawdz('stan programu', stan['wersja'] == p.WERSJA and stan['ustawienia']['glosnosc'] == 55)
try:
    urllib.request.urlopen(urllib.request.Request(baza + '/api/stan', headers={'Host': 'zla-strona.com'}), timeout=5)
    sprawdz('blokada obcych stron', False)
except urllib.error.HTTPError as e:
    sprawdz('blokada obcych stron', e.code == 403)

serwer.shutdown()
time.sleep(1)
print('\nWszystko działa.' if not bledy else f'\nBłędy: {bledy}')
sys.exit(1 if bledy else 0)
