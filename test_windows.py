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

# Mail z nieznanym kodowaniem znaków nie może przerwać czuwania.
zly = (b'From: visit@museum.auschwitz.org\r\nSubject: Odrzucenie zapytania o grupe - 2026-12-09 9:00 - polski\r\n'
       b'Content-Type: text/plain; charset=x-nieznany\r\n\r\n2026-12-09 09:00\r\nZwiedzanie\r\n')
try:
    zd = p.rozpoznaj(zly, 'museum.auschwitz.org')
    sprawdz('mail z nieznanym kodowaniem', zd and zd['typ'] == 'odrzucenie' and zd['godzina'] == '09:00', zd)
except Exception as e:
    sprawdz('mail z nieznanym kodowaniem', False, e)

# Pusty filtr nadawcy nie może przepuszczać wszystkich maili.
sprawdz('pusty nadawca nie łapie spamu', p.rozpoznaj(b'From: sklep@example.com\r\nSubject: Promocja\r\n\r\nx', '') is None)
sprawdz('pusty nadawca w ustawieniach', p.uporzadkuj({**p.DOMYSLNE, 'nadawca': '  '})['nadawca'] == 'museum.auschwitz.org')
sprawdz('błędne liczby w ustawieniach', p.uporzadkuj({**p.DOMYSLNE, 'imap_port': 'abc', 'co_ile_sekund': '1'})
        == {**p.uporzadkuj(dict(p.DOMYSLNE)), 'co_ile_sekund': 3})

# Inna wiadomość z muzeum z terminem w temacie
m2 = EmailMessage()
m2['From'] = 'visit@museum.auschwitz.org'
m2['Subject'] = 'Potwierdzenie rezerwacji - 2026-12-10 10:00 - polski'
m2.set_content('x')
zd = p.rozpoznaj(m2.as_bytes(), 'museum.auschwitz.org')
sprawdz('termin z innej wiadomości', zd and zd['typ'] == 'inny' and zd.get('data') == '2026-12-10', zd)

# Foldery z polskimi znakami (IMAP: zmodyfikowane UTF-7)
sprawdz('folder z polskimi znakami', p.folder_imap('Wysłane') == '"Wys&AUI-ane"', p.folder_imap('Wysłane'))
sprawdz('folder – odczyt nazwy', p.folder_z_imap('Wys&AUI-ane') == 'Wysłane' and p.folder_z_imap('A&-B') == 'A&B')
sprawdz('folder zwykły', p.folder_imap('INBOX') == '"INBOX"')
sprawdz('wersja skryptu Chrome', p.WERSJA_SKRYPTU and p.WERSJA_SKRYPTU.count('.') >= 1, p.WERSJA_SKRYPTU)

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

# Czuwanie na atrapie skrzynki: uszkodzony mail nie może zablokować kolejnego odrzucenia.
class AtrapaIMAP:
    def __init__(self, maile):
        self.maile = maile   # uid → surowy mail
        self.untagged_responses = {'UIDVALIDITY': [b'7']}

    def select(self, folder, readonly=False):
        return ('OK', [b'1']) if folder == '"INBOX"' else ('NO', [b'brak'])

    def list(self):
        return 'OK', [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Wys&AUI-ane"']

    def uid(self, polecenie, *arg):
        if polecenie == 'search':
            od = int(arg[1].split()[1].split(':')[0])
            return 'OK', [' '.join(str(u) for u in sorted(self.maile) if u >= od).encode()]
        return 'OK', [(b'1 (BODY[] {1}', self.maile[int(arg[0])]), b')']


zdarzenia_testu = []
p.reaguj = lambda k, zd: zdarzenia_testu.append(zd)   # bez alarmów i otwierania Chrome w teście
k_test = {**p.konfig(), 'login': 'atrapa@example.com'}
atrapa = AtrapaIMAP({1: m.as_bytes()})
p.sprawdz_folder(k_test, atrapa, 'INBOX')            # pierwsze uruchomienie – stare maile pomijamy
atrapa.maile[2] = zly
atrapa.maile[3] = m.as_bytes().replace(b'2026-12-08', b'2026-12-11')
try:
    p.sprawdz_folder(k_test, atrapa, 'INBOX')
    p.sprawdz_folder(k_test, atrapa, 'INBOX')        # drugi raz – bez powtórnego alarmu
    daty = [z['data'] for z in zdarzenia_testu]
    sprawdz('czuwanie po uszkodzonym mailu', daty == ['2026-12-09', '2026-12-11'], daty)
except Exception as e:
    sprawdz('czuwanie po uszkodzonym mailu', False, e)
try:
    p.sprawdz_folder(k_test, atrapa, 'Wysłane')
    sprawdz('brakujący folder', False)
except Exception as e:
    sprawdz('brakujący folder', 'Wysłane' in str(e) and 'INBOX' in str(e), e)

# Nowe wersje: porównanie wersji i odczyt odpowiedzi GitHuba
sprawdz('porównanie wersji', p.wersja_krotka('v4.1') == p.wersja_krotka('4.1.0') < p.wersja_krotka('4.1.1')
        < p.wersja_krotka('v4.10') and p.wersja_krotka('4.0.1') < p.wersja_krotka(p.WERSJA))
wydanie = {'tag_name': 'v9.1', 'assets': [{'name': 'inny.zip'}, {'name': 'PomocnikRezerwacji.exe'}]}
sprawdz('wydanie z plikiem .exe', p.wersja_wydania(wydanie) == '9.1', p.wersja_wydania(wydanie))
sprawdz('wydanie testowe pomijane', p.wersja_wydania({**wydanie, 'prerelease': True}) is None)
sprawdz('wydanie bez pliku .exe', p.wersja_wydania({'tag_name': 'v9', 'assets': []}) is None)

# Powiadomienie o nowej wersji przychodzi raz na wersję.
powiadomienia = []
p.wyslij_na_telefon = lambda k, tytul, tresc, pilne: powiadomienia.append(tytul)
p.powiadom_system = lambda tytul, tresc: None
nw = p.NoweWersje()
nw.stan.update(najnowsza='9.1', nowsza=True)
nw.powiadom(); nw.powiadom()
nw.stan.update(najnowsza='9.2')
nw.powiadom()
sprawdz('powiadomienie raz na wersję', len(powiadomienia) == 2 and '9.2' in powiadomienia[-1], powiadomienia)

sprawdz('dźwięki systemowe', len(p.dostepne_dzwieki()) > 0, p.dostepne_dzwieki())
p.powiadom_system('Test', 'Test powiadomienia')
p.zagraj_alarm(p.konfig(), 1)

# Lokalny serwer
serwer = p.Serwer(('127.0.0.1', p.PORT), p.Obsluga)
threading.Thread(target=serwer.serve_forever, daemon=True).start()
time.sleep(0.5)
baza = f'http://127.0.0.1:{p.PORT}'


def get(sciezka):
    with urllib.request.urlopen(baza + sciezka, timeout=5) as r:
        return r.status, r.read()


def post(sciezka, dane):
    req = urllib.request.Request(baza + sciezka, data=json.dumps(dane).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:   # odpowiedzi 4xx też mają treść w JSON
        return json.load(e)


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

# Druga kopia programu nie może zająć tego samego portu (inaczej alarmy przychodziłyby podwójnie).
try:
    p.Serwer(('127.0.0.1', p.PORT), p.Obsluga).server_close()
    sprawdz('port na wyłączność', False)
except OSError:
    sprawdz('port na wyłączność', True)

sprawdz('zła godzina próby', not post('/api/proba', {'data': '2026-12-08', 'godzina': '25:00'})['ok'])
sprawdz('zła data próby', not post('/api/proba', {'data': '2026-13-45'})['ok'])
sprawdz('sprawdzenie bez serwera IMAP', 'serwer' in post('/api/sprawdz', {'imap_serwer': '', 'login': 'a@b.pl'})['komunikat'])
sprawdz('zapis błędnej liczby', post('/api/ustawienia', {'co_ile_sekund': 'abc'})['ok'] and p.konfig()['co_ile_sekund'] == 5)
get('/status?v=9.9.9')
stan = json.loads(get('/api/stan')[1])
sprawdz('stan nowej wersji', 'nowa_wersja' in stan and 'powiadamiaj_o_wersji' in stan['ustawienia']
        and stan['nowa_wersja']['adres'].endswith('/releases/latest/download/PomocnikRezerwacji.exe'))
if p.SYSTEM != 'Windows':
    sprawdz('sprawdzanie wersji tylko na Windowsie', not post('/api/sprawdz-wersje', {})['ok'])
sprawdz('wersja skryptu z Chrome', stan['skrypt_chrome_wersja'] == '9.9.9' and stan['skrypt_wersja'] == p.WERSJA_SKRYPTU)

serwer.shutdown()
time.sleep(1)
print('\nWszystko działa.' if not bledy else f'\nBłędy: {bledy}')
sys.exit(1 if bledy else 0)
