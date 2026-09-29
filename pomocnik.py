#!/usr/bin/env python3
"""
Pomocnik rezerwacji – czuwa nad skrzynką pocztową i reaguje na odrzucenia z Muzeum Auschwitz-Birkenau.

Gdy z visit@museum.auschwitz.org przyjdzie „Odrzucenie zapytania o grupę”:
  1. wysyła powiadomienie na telefon (aplikacja ntfy),
  2. pokazuje powiadomienie i gra alarm na komputerze,
  3. otwiera formularz w Chrome – skrypt przeglądarki wypełnia go tym samym terminem.
CAPTCHA i „Wyślij” zostają dla człowieka.

Ustawienia: strona http://127.0.0.1:47631 (otwiera się po uruchomieniu programu).

Parametry:
  (brak)      uruchom i otwórz stronę ustawień
  --w-tle     uruchom bez otwierania strony (autostart)
  --sprawdz   sprawdź logowanie do poczty i pokaż ostatnie maile z muzeum
  --test "2026-12-08 12:00 angielski"   próbny alarm
"""

import base64
import email
import email.policy
import glob
import html
import imaplib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WERSJA = '4.1.0'
NAZWA = 'Pomocnik rezerwacji'
PORT = int(os.environ.get('POMOCNIK_PORT', 47631))  # inny port tylko do testów
SYSTEM = platform.system()  # 'Darwin' | 'Windows' | 'Linux'
SERWIS_HASLA = 'auschwitz-pomocnik'
ADRES_FORMULARZA = 'https://visit.auschwitz.org/formularz.html'
REPO_GITHUB = 'FilipStaryWyga/pomocnik-rezerwacji'   # gdzie sprawdzać nowe wersje (wydania z plikiem .exe)
ADRES_POBRANIA = f'https://github.com/{REPO_GITHUB}/releases/latest/download/PomocnikRezerwacji.exe'
ALARM_PO_MINUTACH_BEZ_POLACZENIA = 10
TEMAT_ODRZUCENIA = re.compile(
    r'Odrzucenie zapytania.*?(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*-\s*(.+?)\s*$', re.I | re.S)


# ---------- Ścieżki ----------

def katalog_programu():
    """Katalog z plikami programu (skrypt przeglądarki)."""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def katalog_danych():
    if SYSTEM == 'Windows':
        baza = os.environ.get('APPDATA') or os.path.expanduser('~')
    elif SYSTEM == 'Darwin':
        baza = os.path.expanduser('~/Library/Application Support')
    else:
        baza = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    d = os.path.join(baza, 'PomocnikRezerwacji')
    os.makedirs(d, exist_ok=True)
    return d


DANE = katalog_danych()
PLIK_KONFIG = os.path.join(DANE, 'ustawienia.json')
PLIK_STAN = os.path.join(DANE, 'stan.json')
PLIK_LOG = os.path.join(DANE, 'pomocnik.log')
PLIK_HASEL = os.path.join(DANE, 'hasla.bin')  # tylko Windows/Linux; na Macu hasło jest w Pęku kluczy
PLIK_SKRYPTU = os.path.join(katalog_programu(), 'pomocnik.user.js')

blokada = threading.Lock()


def log(*a):
    linia = datetime.now().strftime('%Y-%m-%d %H:%M:%S ') + ' '.join(str(x) for x in a)
    if sys.stdout:
        try:
            print(linia, flush=True)
        except Exception:
            pass
    try:
        if os.path.exists(PLIK_LOG) and os.path.getsize(PLIK_LOG) > 1_000_000:
            os.replace(PLIK_LOG, PLIK_LOG + '.1')
        with open(PLIK_LOG, 'a', encoding='utf-8') as f:
            f.write(linia + '\n')
    except OSError:
        pass


def ostatnie_linie_logu(n=60):
    try:
        with open(PLIK_LOG, encoding='utf-8') as f:
            return f.readlines()[-n:]
    except OSError:
        return []


# ---------- Ustawienia i stan ----------

DOMYSLNE = {
    'imap_serwer': '',
    'imap_port': 993,
    'login': '',
    'foldery': ['INBOX'],
    'nadawca': 'museum.auschwitz.org',
    'akceptuj_przekazane': False,   # tryb testowy: maile „Fwd:” z muzeum przesłane z innej skrzynki
    'co_ile_sekund': 5,
    'telefon': True,
    'ntfy_serwer': 'https://ntfy.sh',
    'ntfy_temat': '',
    'otwieraj_formularz': True,
    'autostart': True,
    'dzwiek': 'Sosumi' if SYSTEM == 'Darwin' else 'Alarm01',
    'glosnosc': 80,                 # 0–100
    'powtorzenia': 3,
    'alarm_bez_polaczenia': True,   # powiadom, gdy program nie może połączyć się z pocztą przez dłuższy czas
    'powiadamiaj_o_wersji': True,   # Windows: powiadom, gdy na GitHubie jest nowsza wersja do pobrania
}


def wczytaj_json(sciezka, domyslne):
    try:
        with open(sciezka, encoding='utf-8') as f:
            dane = json.load(f)
        return dane if isinstance(dane, type(domyslne)) else domyslne
    except FileNotFoundError:
        return domyslne
    except (ValueError, OSError) as e:
        # Uszkodzony plik (np. po zaniku prądu) – zachowaj go do wglądu zamiast po cichu nadpisywać.
        try:
            shutil.copy2(sciezka, sciezka + '.uszkodzony')
        except OSError:
            pass
        log('Uszkodzony plik', os.path.basename(sciezka), '–', e)
        return domyslne


def zapisz_json(sciezka, dane):
    tmp = sciezka + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(dane, f, ensure_ascii=False, indent=2)
    os.replace(tmp, sciezka)


def konfig():
    k = uporzadkuj({**DOMYSLNE, **wczytaj_json(PLIK_KONFIG, {})})
    if not k['ntfy_temat']:
        k['ntfy_temat'] = 'pomocnik-' + secrets.token_hex(8)
        zapisz_json(PLIK_KONFIG, k)
    return k


def liczba(wartosc, domyslna, od, do):
    """Liczba całkowita z pola formularza; puste lub błędne pole daje wartość domyślną."""
    try:
        return min(do, max(od, int(str(wartosc).strip() or domyslna)))
    except (TypeError, ValueError):
        return domyslna


def uporzadkuj(k):
    """Poprawia wartości z formularza (typy, zakresy, puste pola)."""
    k['imap_serwer'] = str(k['imap_serwer'] or '').strip()
    k['login'] = str(k['login'] or '').strip()
    k['imap_port'] = liczba(k['imap_port'], 993, 1, 65535)
    k['co_ile_sekund'] = liczba(k['co_ile_sekund'], 5, 3, 3600)
    k['glosnosc'] = liczba(k['glosnosc'], 80, 0, 100)
    k['powtorzenia'] = liczba(k['powtorzenia'], 3, 1, 20)
    if isinstance(k['foldery'], str):
        k['foldery'] = [f.strip() for f in k['foldery'].split(',') if f.strip()]
    k['foldery'] = k['foldery'] or ['INBOX']
    # Pusty nadawca pasowałby do każdego maila – wtedy alarmy wywoływałby spam i newslettery.
    k['nadawca'] = str(k['nadawca'] or '').strip() or DOMYSLNE['nadawca']
    return k


def zapisz_konfig(zmiany):
    with blokada:
        k = konfig()
        for klucz, wartosc in zmiany.items():
            if klucz in DOMYSLNE:
                k[klucz] = wartosc
        uporzadkuj(k)
        zapisz_json(PLIK_KONFIG, k)
        return k


# ---------- Bezpieczne przechowywanie hasła ----------

if SYSTEM == 'Windows':
    import ctypes
    import ctypes.wintypes as wt

    class _BLOB(ctypes.Structure):
        _fields_ = [('cbData', wt.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

    def _dpapi(dane, szyfruj):
        bufor = ctypes.create_string_buffer(dane, len(dane))
        wej = _BLOB(len(dane), ctypes.cast(bufor, ctypes.POINTER(ctypes.c_char)))
        wyj = _BLOB()
        f = ctypes.windll.crypt32.CryptProtectData if szyfruj else ctypes.windll.crypt32.CryptUnprotectData
        if not f(ctypes.byref(wej), None, None, None, None, 0, ctypes.byref(wyj)):
            raise OSError('Błąd szyfrowania hasła (DPAPI)')
        try:
            return ctypes.string_at(wyj.pbData, wyj.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(wyj.pbData)


def _hasla_plik():
    try:
        with open(PLIK_HASEL, 'rb') as f:
            surowe = f.read()
        if SYSTEM == 'Windows':
            surowe = _dpapi(surowe, False)
        return json.loads(base64.b64decode(surowe))
    except Exception:
        return {}


def _hasla_plik_zapisz(hasla):
    surowe = base64.b64encode(json.dumps(hasla).encode())
    if SYSTEM == 'Windows':
        surowe = _dpapi(surowe, True)
    with open(PLIK_HASEL, 'wb') as f:
        f.write(surowe)
    if SYSTEM != 'Windows':
        os.chmod(PLIK_HASEL, 0o600)


def haslo_zapisz(login, haslo):
    _czy_haslo.clear()
    if SYSTEM == 'Darwin':
        r = subprocess.run(['security', 'add-generic-password', '-U', '-s', SERWIS_HASLA, '-a', login, '-w', haslo],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise OSError('Nie udało się zapisać hasła w Pęku kluczy: ' + r.stderr.strip())
    else:
        h = _hasla_plik()
        h[login] = haslo
        _hasla_plik_zapisz(h)


def haslo_odczytaj(login):
    if not login:
        return None
    if SYSTEM == 'Darwin':
        r = subprocess.run(['security', 'find-generic-password', '-s', SERWIS_HASLA, '-a', login, '-w'],
                           capture_output=True, text=True)
        return r.stdout.rstrip('\n') if r.returncode == 0 else None
    return _hasla_plik().get(login)


_czy_haslo = {}   # login → (czy jest hasło, kiedy sprawdzono)


def ma_haslo(login):
    """Czy jest zapisane hasło – z krótką pamięcią, bo strona ustawień pyta co 3 sekundy
    (na Macu każde sprawdzenie to osobny proces „security”)."""
    zapamietane = _czy_haslo.get(login)
    if zapamietane and time.time() - zapamietane[1] < 30:
        return zapamietane[0]
    wynik = bool(haslo_odczytaj(login))
    _czy_haslo[login] = (wynik, time.time())
    return wynik


# ---------- Autostart ----------

def polecenie_startowe():
    if os.environ.get('POMOCNIK_LAUNCHER'):          # uruchomione z aplikacji .app na Macu
        return [os.path.abspath(os.environ['POMOCNIK_LAUNCHER']), '--w-tle']
    if getattr(sys, 'frozen', False):                # .exe na Windowsie
        return [sys.executable, '--w-tle']
    return [sys.executable, os.path.abspath(__file__), '--w-tle']


PLIST = os.path.expanduser('~/Library/LaunchAgents/pl.pomocnik-rezerwacji.plist')
KLUCZ_RUN = r'Software\Microsoft\Windows\CurrentVersion\Run'


def autostart_ustaw(wlaczony):
    try:
        if SYSTEM == 'Darwin':
            if wlaczony:
                argumenty = ''.join(f'<string>{html.escape(a)}</string>' for a in polecenie_startowe())
                os.makedirs(os.path.dirname(PLIST), exist_ok=True)
                with open(PLIST, 'w', encoding='utf-8') as f:
                    f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                            '<plist version="1.0"><dict>'
                            '<key>Label</key><string>pl.pomocnik-rezerwacji</string>'
                            f'<key>ProgramArguments</key><array>{argumenty}</array>'
                            '<key>RunAtLoad</key><true/>'
                            '</dict></plist>\n')
            elif os.path.exists(PLIST):
                os.remove(PLIST)
        elif SYSTEM == 'Windows':
            import winreg
            # CreateKeyEx otwiera klucz albo go tworzy (na świeżym koncie może jeszcze nie istnieć).
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KLUCZ_RUN, 0, winreg.KEY_SET_VALUE) as klucz:
                if wlaczony:
                    winreg.SetValueEx(klucz, 'PomocnikRezerwacji', 0, winreg.REG_SZ,
                                      subprocess.list2cmdline(polecenie_startowe()))
                else:
                    try:
                        winreg.DeleteValue(klucz, 'PomocnikRezerwacji')
                    except FileNotFoundError:
                        pass
    except Exception as e:
        log('Autostart – błąd:', e)


def autostart_wlaczony():
    if SYSTEM == 'Darwin':
        return os.path.exists(PLIST)
    if SYSTEM == 'Windows':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KLUCZ_RUN) as klucz:
                winreg.QueryValueEx(klucz, 'PomocnikRezerwacji')
                return True
        except OSError:
            return False
    return False


# ---------- Instalacja i odinstalowanie ----------

KATALOG_INSTALACJI = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'PomocnikRezerwacji')
EXE_INSTALACJI = os.path.join(KATALOG_INSTALACJI, 'PomocnikRezerwacji.exe')


def powershell(skrypt):
    return subprocess.run(['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command', skrypt],
                          capture_output=True, text=True, creationflags=0x08000000)


def skroty_windows(utworz):
    """Skróty w menu Start i na pulpicie."""
    cel = EXE_INSTALACJI.replace("'", "''")
    powershell(
        "$sciezki = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop')) | "
        "ForEach-Object { Join-Path $_ 'Pomocnik rezerwacji.lnk' };"
        + ("$sh = New-Object -ComObject WScript.Shell;"
           f"foreach ($p in $sciezki) {{ $s = $sh.CreateShortcut($p); $s.TargetPath = '{cel}'; $s.Save() }}"
           if utworz else
           "foreach ($p in $sciezki) { Remove-Item -LiteralPath $p -ErrorAction SilentlyContinue }"))


def zainstaluj_windows():
    """Pobrany plik .exe instaluje się sam: kopiuje na stałe miejsce, tworzy skróty i uruchamia kopię.
    Uruchomienie nowszej wersji pobranego pliku podmienia zainstalowaną (aktualizacja).
    Zwraca True, gdy ten proces ma się zakończyć, bo działa już zainstalowana kopia."""
    if SYSTEM != 'Windows' or not getattr(sys, 'frozen', False) or not os.environ.get('LOCALAPPDATA'):
        return False
    if os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(EXE_INSTALACJI):
        return False

    if juz_dziala():  # działa starsza wersja – zamknij ją przed podmianą pliku
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{PORT}/api/zakoncz', data=b'{}',
                                         headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass
        for _ in range(40):
            if not juz_dziala():
                break
            time.sleep(0.25)

    os.makedirs(KATALOG_INSTALACJI, exist_ok=True)
    for _ in range(40):
        try:
            shutil.copy2(sys.executable, EXE_INSTALACJI)
            break
        except PermissionError:   # stary proces jeszcze zwalnia plik
            time.sleep(0.25)
    else:
        return False   # nie udało się – działaj z miejsca, z którego uruchomiono
    skroty_windows(True)
    log('Zainstalowano w', KATALOG_INSTALACJI)

    # Nowa, niezależna kopia programu (bez dziedziczenia katalogu tymczasowego PyInstallera).
    env = {k: v for k, v in os.environ.items() if not k.startswith('_MEI') and not k.startswith('_PYI')}
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    subprocess.Popen([EXE_INSTALACJI] + sys.argv[1:], env=env, close_fds=True,
                     creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return True


def odinstaluj():
    """Usuwa autostart, skróty, ustawienia i zapisane hasła. Program zamyka się po wykonaniu."""
    k = konfig()
    autostart_ustaw(False)
    if SYSTEM == 'Darwin':
        subprocess.run(['security', 'delete-generic-password', '-s', SERWIS_HASLA, '-a', k['login']],
                       capture_output=True)
    shutil.rmtree(DANE, ignore_errors=True)
    if SYSTEM == 'Windows':
        skroty_windows(False)
        if getattr(sys, 'frozen', False) and os.path.isdir(KATALOG_INSTALACJI):
            # Plik programu można usunąć dopiero po jego zamknięciu.
            subprocess.Popen(f'cmd /c ping -n 4 127.0.0.1 >nul & rmdir /s /q "{KATALOG_INSTALACJI}"',
                             creationflags=0x08000000 | 0x00000008)


# ---------- Dźwięk alarmu ----------

def dostepne_dzwieki():
    """Słownik nazwa → ścieżka pliku z dźwiękami systemowymi."""
    if SYSTEM == 'Darwin':
        wzorce = ['/System/Library/Sounds/*.aiff']
    elif SYSTEM == 'Windows':
        wzorce = [os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Media', '*.wav')]
    else:
        wzorce = ['/usr/share/sounds/freedesktop/stereo/*.oga']
    pliki = sorted(p for w in wzorce for p in glob.glob(w))
    return {os.path.splitext(os.path.basename(p))[0]: p for p in pliki}


def zagraj_alarm(k, powtorzenia=None):
    dzwieki = dostepne_dzwieki()
    plik = dzwieki.get(k['dzwiek']) or next(iter(dzwieki.values()), None)
    if not plik:
        return
    ile = powtorzenia or k['powtorzenia']
    glosnosc = k['glosnosc'] / 100

    def graj():
        try:
            if SYSTEM == 'Darwin':
                for _ in range(ile):
                    subprocess.run(['afplay', '-v', f'{glosnosc:.2f}', plik])
            elif SYSTEM == 'Windows':
                ps = ('Add-Type -AssemblyName PresentationCore;'
                      f"$p = New-Object System.Windows.Media.MediaPlayer; $p.Volume = {glosnosc:.2f};"
                      f"for ($i = 0; $i -lt {ile}; $i++) {{ $p.Open([uri]'{plik}'); $p.Play(); Start-Sleep -Milliseconds 300;"
                      " while (-not $p.NaturalDuration.HasTimeSpan) { Start-Sleep -Milliseconds 50 };"
                      " Start-Sleep -Milliseconds ([int]$p.NaturalDuration.TimeSpan.TotalMilliseconds); $p.Close() }")
                subprocess.run(['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command', ps],
                               creationflags=0x08000000)
            else:
                for _ in range(ile):
                    subprocess.run(['paplay', plik])
        except Exception as e:
            log('Dźwięk – błąd:', e)

    threading.Thread(target=graj, daemon=True).start()


# ---------- Rozpoznawanie maili ----------

def tekst_maila(msg):
    try:
        czesc = msg.get_body(preferencelist=('plain', 'html'))
    except Exception:
        return ''
    if czesc is None:
        return ''
    try:
        tresc = czesc.get_content()
    except Exception:
        # Nieznane lub błędne kodowanie znaków – odczytaj, co się da, zamiast przerywać czuwanie.
        surowe = czesc.get_payload(decode=True) or b''
        tresc = surowe.decode('utf-8', 'replace') if isinstance(surowe, bytes) else str(surowe)
    if not isinstance(tresc, str):
        return ''
    if czesc.get_content_type() == 'text/html':
        tresc = re.sub(r'(?i)<br\s*/?>|</p>|</div>', '\n', tresc)
        tresc = html.unescape(re.sub(r'<[^>]+>', '', tresc))
    return tresc


def rozpoznaj(surowy, nadawca_filtr, przekazane=False):
    """Zwraca słownik zdarzenia albo None, jeśli to nie jest mail z muzeum."""
    msg = email.message_from_bytes(surowy, policy=email.policy.default)
    nadawca_filtr = (nadawca_filtr or DOMYSLNE['nadawca']).lower()

    def naglowek(nazwa):
        try:
            return str(msg.get(nazwa, ''))
        except Exception:   # błędnie zakodowany nagłówek
            return ''

    nadawca = naglowek('From')
    if nadawca_filtr not in nadawca.lower():
        if not (przekazane and nadawca_filtr in tekst_maila(msg).lower()):
            return None
    temat = ' '.join(naglowek('Subject').split())
    zd = {'temat_maila': temat, 'nadawca': nadawca, 'message_id': naglowek('Message-ID')}

    m = TEMAT_ODRZUCENIA.search(temat)
    if not m:
        zd['typ'] = 'inny'
        # Termin z tematu (np. potwierdzenie rezerwacji) – skrypt w Chrome pozwoli oznaczyć go w dzienniku.
        d = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', temat)
        if d:
            zd['data'] = d.group(1)
        return zd

    data, godzina, jezyk = m.group(1), m.group(2).zfill(5), m.group(3).strip()
    zd.update(typ='odrzucenie', data=data, godzina=godzina, jezyk=jezyk, rodzaj='')
    # Rodzaj zwiedzania jest w treści: linia z datą i godziną, pod nią rodzaj, pod nim język.
    linie = [l.strip() for l in tekst_maila(msg).splitlines() if l.strip()]
    for i, l in enumerate(linie):
        if l.startswith(f'{data} {godzina}') and i + 1 < len(linie):
            zd['rodzaj'] = linie[i + 1]
            break
    return zd


# ---------- Powiadomienia ----------

def wyslij_na_telefon(k, tytul, tresc, pilne):
    if not (k['telefon'] and k['ntfy_temat']):
        return True
    try:
        body = json.dumps({'topic': k['ntfy_temat'], 'title': tytul, 'message': tresc,
                           'priority': 5 if pilne else 3,
                           'tags': ['rotating_light'] if pilne else ['envelope']}).encode()
        req = urllib.request.Request(k['ntfy_serwer'], data=body, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception as e:
        log('Nie udało się wysłać powiadomienia na telefon:', e)
        return False


def powiadom_system(tytul, tresc):
    try:
        if SYSTEM == 'Darwin':
            esc = lambda s: s.replace('\\', '\\\\').replace('"', '\\"')
            subprocess.Popen(['osascript', '-e', f'display notification "{esc(tresc)}" with title "{esc(tytul)}"'])
        elif SYSTEM == 'Windows':
            esc = lambda s: s.replace("'", "''").replace('<', ' ').replace('>', ' ').replace('&', 'i')
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
                "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
                f"$x.LoadXml('<toast scenario=\"reminder\"><visual><binding template=\"ToastGeneric\"><text>{esc(tytul)}</text>"
                f"<text>{esc(tresc)}</text></binding></visual><audio silent=\"true\"/>"
                "<actions><action content=\"OK\" arguments=\"ok\"/></actions></toast>');"
                "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
                "[Windows.UI.Notifications.ToastNotification]::new($x))"
            )
            subprocess.Popen(['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command', ps],
                             creationflags=0x08000000)
    except Exception as e:
        log('Powiadomienie systemowe – błąd:', e)


def znajdz_chrome():
    if SYSTEM == 'Darwin':
        return 'Google Chrome' if os.path.exists('/Applications/Google Chrome.app') else None
    if SYSTEM == 'Windows':
        for baza in (os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)'), os.environ.get('LOCALAPPDATA')):
            if baza:
                p = os.path.join(baza, 'Google', 'Chrome', 'Application', 'chrome.exe')
                if os.path.exists(p):
                    return p
    return shutil.which('google-chrome')


def otworz_w_chrome(url):
    chrome = znajdz_chrome()
    try:
        if chrome and SYSTEM == 'Darwin':
            subprocess.Popen(['open', '-a', chrome, url])
        elif chrome:
            subprocess.Popen([chrome, url])
        else:
            webbrowser.open(url)
    except Exception:
        webbrowser.open(url)


def reaguj(k, zd):
    if zd['typ'] == 'odrzucenie':
        d = datetime.strptime(zd['data'], '%Y-%m-%d').strftime('%d.%m.%Y')
        tytul = ('PRÓBA: ' if str(zd.get('id', '')).startswith('proba') else '') + f'Odrzucono {d}, {zd["godzina"]}'
        tresc = (f'{zd["jezyk"].capitalize()} · {zd["rodzaj"] or "zwiedzanie grupowe"}. '
                 'Formularz jest gotowy na komputerze – zaznacz CAPTCHA i wyślij.')
        wyslij_na_telefon(k, tytul, tresc, True)
        powiadom_system(tytul, tresc)
        zagraj_alarm(k)
        if k['otwieraj_formularz']:
            otworz_w_chrome(f'{ADRES_FORMULARZA}#pomocnik={zd["id"]}')
    else:
        wyslij_na_telefon(k, 'Nowa wiadomość z muzeum', zd['temat_maila'], False)
        powiadom_system('Nowa wiadomość z muzeum', zd['temat_maila'])


def nowe_zdarzenie(k, zd):
    zd['czas'] = datetime.now().astimezone().isoformat(timespec='seconds')
    with blokada:
        stan = wczytaj_json(PLIK_STAN, {})
        zdarzenia = stan.setdefault('zdarzenia', [])
        if zd.get('message_id') and any(z.get('message_id') == zd['message_id'] for z in zdarzenia):
            return
        zdarzenia.append(zd)
        stan['zdarzenia'] = zdarzenia[-100:]
        zapisz_json(PLIK_STAN, stan)
    log('Nowy mail:', zd['typ'], zd.get('data', ''), zd.get('godzina', ''), zd.get('jezyk', ''), '|', zd['temat_maila'])
    try:
        reaguj(k, zd)
    except Exception as e:
        log('Błąd podczas alarmu:', e)


# ---------- Czuwanie nad pocztą ----------

def przyjazny_blad(e):
    t = str(e)
    tl = t.lower()
    if 'application-specific password' in tl or 'app password' in tl:
        return 'Gmail wymaga hasła do aplikacji (myaccount.google.com/apppasswords), a nie zwykłego hasła do konta.'
    if 'AUTHENTICATIONFAILED' in t or 'invalid credentials' in tl or 'authentication failed' in tl \
            or 'login failed' in tl or 'authenticate failed' in tl:
        return ('Nieprawidłowy login lub hasło. W Gmailu użyj hasła do aplikacji; '
                'w Outlook/Microsoft 365 logowanie hasłem przez IMAP bywa wyłączone przez administratora.')
    if 'certificate_verify_failed' in tl or 'certificate verify failed' in tl:
        return ('Nie można sprawdzić certyfikatu serwera poczty. Sprawdź datę i godzinę w komputerze '
                'albo program antywirusowy skanujący pocztę.')
    if 'wrong_version_number' in tl or 'wrong version number' in tl or 'unknown protocol' in tl:
        return 'Serwer nie obsługuje szyfrowanego połączenia na tym porcie. Zwykle właściwy port to 993.'
    if isinstance(e, ConnectionRefusedError) or 'connection refused' in tl:
        return 'Serwer poczty odrzucił połączenie. Sprawdź adres serwera IMAP i port (zwykle 993).'
    if isinstance(e, TimeoutError) or 'timed out' in tl:
        return 'Serwer poczty nie odpowiada. Sprawdź adres serwera i połączenie z internetem.'
    if 'nodename nor servname' in t or 'getaddrinfo' in t or 'Name or service not known' in t \
            or 'No address associated' in t or 'Errno 11001' in t:
        return 'Nie znaleziono serwera poczty. Sprawdź adres serwera IMAP i połączenie z internetem.'
    return t


def polacz(k, haslo=None):
    if not k['imap_serwer']:
        raise imaplib.IMAP4.error('Podaj adres serwera IMAP (np. imap.gmail.com).')
    if not k['login']:
        raise imaplib.IMAP4.error('Podaj adres e-mail skrzynki.')
    haslo = haslo if haslo is not None else haslo_odczytaj(k['login'])
    if not haslo:
        raise imaplib.IMAP4.error('Brak zapisanego hasła do skrzynki.')
    m = imaplib.IMAP4_SSL(k['imap_serwer'], int(k['imap_port']), timeout=30)
    try:
        m.login(k['login'], haslo)
    except Exception:
        try:
            m.shutdown()
        except Exception:
            pass
        raise
    return m


def policz_maile_z_muzeum(k, m):
    """Liczba maili od muzeum w obserwowanych folderach (do sprawdzenia ustawień)."""
    kryterium = 'TEXT' if k['akceptuj_przekazane'] else 'FROM'
    nadawca = '"' + k['nadawca'].replace('\\', '\\\\').replace('"', '\\"') + '"'
    liczby = {}
    for folder in k['foldery']:
        otworz_folder(m, folder)
        _, d = m.uid('search', None, kryterium, nadawca)
        liczby[folder] = (d[0] or b'').split()
    return liczby


def folder_imap(nazwa):
    """Nazwa folderu w postaci wymaganej przez IMAP (zmodyfikowane UTF-7, w cudzysłowie),
    żeby działały też foldery z polskimi znakami, np. „Zgłoszenia”."""
    wynik, bufor = [], []

    def zrzuc():
        if bufor:
            b64 = base64.b64encode(''.join(bufor).encode('utf-16-be')).decode().rstrip('=')
            wynik.append('&' + b64.replace('/', ',') + '-')
            bufor.clear()

    for znak in nazwa:
        if 0x20 <= ord(znak) <= 0x7e:
            zrzuc()
            wynik.append('&-' if znak == '&' else znak)
        else:
            bufor.append(znak)
    zrzuc()
    return '"' + ''.join(wynik).replace('\\', '\\\\').replace('"', '\\"') + '"'


def folder_z_imap(nazwa):
    """Odwrotność folder_imap (bez cudzysłowów) – do pokazania listy folderów."""
    def zamien(m):
        if m.group(1) == '':
            return '&'
        b64 = m.group(1).replace(',', '/')
        return base64.b64decode(b64 + '=' * (-len(b64) % 4)).decode('utf-16-be', 'replace')
    return re.sub(r'&([^-]*)-', zamien, nazwa)


def lista_folderow(m):
    typ, dane = m.list()
    if typ != 'OK':
        return []
    foldery = []
    for linia in dane or []:
        if not isinstance(linia, bytes):
            continue
        r = re.match(rb'\((.*?)\) (?:"(?:\\.|[^"])*"|NIL) (.+)$', linia)
        if not r or b'\\Noselect' in r.group(1):
            continue
        nazwa = r.group(2).decode('ascii', 'replace').strip()
        if nazwa.startswith('"') and nazwa.endswith('"'):
            nazwa = nazwa[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        foldery.append(folder_z_imap(nazwa))
    return foldery


def otworz_folder(m, folder):
    typ, _ = m.select(folder_imap(folder), readonly=True)
    if typ != 'OK':
        dostepne = lista_folderow(m)
        podpowiedz = f' Dostępne foldery: {", ".join(dostepne[:15])}.' if dostepne else ''
        raise imaplib.IMAP4.error(f'W skrzynce nie ma folderu „{folder}”.{podpowiedz}')


def sprawdz_folder(k, m, folder):
    otworz_folder(m, folder)
    uidvalidity = m.untagged_responses.get('UIDVALIDITY', [b'0'])[-1].decode()
    klucz = f'{k["login"]}|{folder}|{uidvalidity}'

    with blokada:
        ostatni = wczytaj_json(PLIK_STAN, {}).get('ostatni_uid', {}).get(klucz)

    def zapamietaj(uid):
        with blokada:
            stan = wczytaj_json(PLIK_STAN, {})
            stan.setdefault('ostatni_uid', {})[klucz] = uid
            zapisz_json(PLIK_STAN, stan)

    _, dane = m.uid('search', None, f'UID {(ostatni or 0) + 1}:*')
    uidy = [int(u) for u in (dane[0] or b'').split()]
    if ostatni is None:
        zapamietaj(max(uidy, default=0))   # pierwsze uruchomienie: stare maile pomijamy, czuwamy od teraz
        return
    for uid in sorted(u for u in uidy if u > ostatni):
        _, d = m.uid('fetch', str(uid), '(BODY.PEEK[])')  # PEEK = nie oznacza jako przeczytane
        surowy = next((c[1] for c in d if isinstance(c, tuple)), None)
        if surowy:
            try:
                zd = rozpoznaj(surowy, k['nadawca'], k['akceptuj_przekazane'])
            except Exception as e:
                # Jeden nietypowy mail nie może zatrzymać czuwania – pomijamy go i idziemy dalej.
                log(f'Nie udało się odczytać maila {uid} w folderze {folder}:', e)
                zd = None
            if zd:
                zd['id'] = f'{uidvalidity}-{uid}'
                nowe_zdarzenie(k, zd)
        zapamietaj(uid)   # po każdym mailu, żeby po przerwaniu połączenia nie alarmować drugi raz


class Czuwanie(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.restart = threading.Event()
        self.stan = {'polaczono': False, 'blad': None, 'ostatnie_sprawdzenie': None, 'login': ''}
        self.blad_od = None          # od kiedy trwa brak połączenia
        self.zgloszono_awarie = False

    def _awaria(self, k):
        """Gdy poczta nie działa dłużej niż kilka minut – powiadom, żeby nikt nie przegapił odrzuceń."""
        if self.blad_od is None:
            self.blad_od = time.time()
        if (k['alarm_bez_polaczenia'] and not self.zgloszono_awarie
                and time.time() - self.blad_od > ALARM_PO_MINUTACH_BEZ_POLACZENIA * 60):
            self.zgloszono_awarie = True
            tresc = f'Od {ALARM_PO_MINUTACH_BEZ_POLACZENIA} minut nie można sprawdzić poczty: {self.stan["blad"]}'
            wyslij_na_telefon(k, 'Pomocnik nie sprawdza poczty', tresc, False)
            powiadom_system('Pomocnik nie sprawdza poczty', tresc)

    def _polaczono(self, k):
        if self.zgloszono_awarie:
            wyslij_na_telefon(k, 'Pomocnik znów czuwa', f'Połączenie ze skrzynką {k["login"]} przywrócone.', False)
        self.blad_od = None
        self.zgloszono_awarie = False

    def run(self):
        przerwa = 5
        while True:
            k = konfig()
            self.stan['login'] = k['login']
            if not (k['imap_serwer'] and k['login'] and haslo_odczytaj(k['login'])):
                self.stan.update(polaczono=False, blad='Uzupełnij ustawienia skrzynki pocztowej.')
                self.blad_od = None
                self.restart.wait()
                self.restart.clear()
                continue
            m = None
            try:
                m = polacz(k)
                log('Połączono ze skrzynką', k['login'])
                self.stan.update(polaczono=True, blad=None)
                self._polaczono(k)
                przerwa = 5
                while not self.restart.is_set():
                    for folder in k['foldery']:
                        sprawdz_folder(k, m, folder)
                    self.stan['ostatnie_sprawdzenie'] = datetime.now().astimezone().isoformat(timespec='seconds')
                    self.restart.wait(k['co_ile_sekund'])
            except Exception as e:
                self.stan.update(polaczono=False, blad=przyjazny_blad(e))
                log('Poczta – błąd:', self.stan['blad'])
                self._awaria(k)
                self.restart.wait(przerwa)
                przerwa = min(przerwa * 2, 60)
            finally:
                if m is not None:
                    try:
                        m.logout()
                    except Exception:
                        pass
            self.restart.clear()


czuwanie = Czuwanie()


# ---------- Nowe wersje (Windows) ----------
# Program tylko sprawdza i powiadamia. Niczego sam nie pobiera ani nie uruchamia – nową wersję
# pobiera człowiek (link na stronie ustawień), a uruchomiony plik podmienia zainstalowany.

NAZWA_EXE = 'PomocnikRezerwacji.exe'


def wersja_krotka(w):
    """„v4.1” → (4, 1, 0, 0), żeby porównywać wersje liczbowo."""
    liczby = [int(x) for x in re.findall(r'\d+', str(w or ''))[:4]]
    return tuple(liczby + [0] * (4 - len(liczby)))


def wersja_wydania(dane):
    """Wersja z odpowiedzi GitHuba o najnowszym wydaniu – tylko gotowego i z plikiem .exe."""
    if not isinstance(dane, dict) or dane.get('draft') or dane.get('prerelease'):
        return None
    if not any(plik.get('name') == NAZWA_EXE for plik in dane.get('assets') or []):
        return None
    return str(dane.get('tag_name') or '').lstrip('vV') or None


def sprawdzaj_wersje():
    """Wydania na GitHubie to plik .exe – sprawdzanie ma sens tylko w wersji na Windows."""
    return SYSTEM == 'Windows' and getattr(sys, 'frozen', False)


class NoweWersje(threading.Thread):
    CO_ILE = 6 * 3600

    def __init__(self):
        super().__init__(daemon=True)
        self.teraz = threading.Event()
        self.stan = {'mozliwe': sprawdzaj_wersje(), 'najnowsza': None, 'nowsza': False,
                     'sprawdzono': None, 'blad': None, 'adres': ADRES_POBRANIA}

    def sprawdz(self):
        req = urllib.request.Request(f'https://api.github.com/repos/{REPO_GITHUB}/releases/latest',
                                     headers={'User-Agent': f'PomocnikRezerwacji/{WERSJA}',
                                              'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(req, timeout=20) as r:
            najnowsza = wersja_wydania(json.load(r))
        self.stan.update(najnowsza=najnowsza,
                         nowsza=bool(najnowsza) and wersja_krotka(najnowsza) > wersja_krotka(WERSJA),
                         sprawdzono=datetime.now().astimezone().isoformat(timespec='seconds'), blad=None)

    def powiadom(self):
        """Raz na każdą nową wersję: telefon i powiadomienie na komputerze."""
        k = konfig()
        if not (self.stan['nowsza'] and k['powiadamiaj_o_wersji']):
            return
        with blokada:
            stan = wczytaj_json(PLIK_STAN, {})
            if stan.get('powiadomiono_o_wersji') == self.stan['najnowsza']:
                return
            stan['powiadomiono_o_wersji'] = self.stan['najnowsza']
            zapisz_json(PLIK_STAN, stan)
        tytul = f'Nowa wersja Pomocnika rezerwacji: {self.stan["najnowsza"]}'
        tresc = 'Pobierz ją na stronie ustawień programu i uruchom pobrany plik – ustawienia zostaną zachowane.'
        log(tytul)
        wyslij_na_telefon(k, tytul, tresc, False)
        powiadom_system(tytul, tresc)

    def run(self):
        if not self.stan['mozliwe']:
            return
        if self.teraz.wait(120):        # pierwsze sprawdzenie chwilę po starcie
            self.teraz.clear()
        while True:
            try:
                self.sprawdz()
                self.powiadom()
            except Exception as e:
                self.stan['blad'] = przyjazny_blad(e)
                log('Sprawdzanie nowej wersji – błąd:', self.stan['blad'])
            self.teraz.wait(self.CO_ILE)
            self.teraz.clear()


nowe_wersje = NoweWersje()
# Kiedy skrypt w Chrome ostatnio odezwał się do programu i w jakiej wersji.
skrypt_chrome = {'ostatnio': None, 'wersja': None}


def wersja_skryptu():
    """Wersja skryptu dołączonego do programu (z nagłówka @version)."""
    try:
        with open(PLIK_SKRYPTU, encoding='utf-8') as f:
            m = re.search(r'@version\s+(\S+)', f.read(2000))
        return m.group(1) if m else None
    except OSError:
        return None


WERSJA_SKRYPTU = wersja_skryptu()


# ---------- Lokalny serwer: strona ustawień + API dla skryptu w Chrome ----------

def stan_publiczny():
    k = konfig()
    with blokada:
        zdarzenia = wczytaj_json(PLIK_STAN, {}).get('zdarzenia', [])
    return {
        'wersja': WERSJA, 'system': SYSTEM,
        **czuwanie.stan,
        'ustawienia': {kl: k[kl] for kl in DOMYSLNE},
        'ma_haslo': ma_haslo(k['login']),
        'autostart': autostart_wlaczony(),
        'dzwieki': list(dostepne_dzwieki()),
        'skrypt_chrome': skrypt_chrome['ostatnio'],
        'skrypt_chrome_wersja': skrypt_chrome['wersja'],
        'skrypt_wersja': WERSJA_SKRYPTU,
        'chrome': bool(znajdz_chrome()),
        'nowa_wersja': nowe_wersje.stan,
        'zdarzenia': list(reversed(zdarzenia[-30:])),
    }


def kontakt_skryptu(zapytanie):
    """Zapamiętuje, że skrypt w Chrome działa (i w jakiej wersji – starsze wersje jej nie podają)."""
    skrypt_chrome['ostatnio'] = datetime.now().astimezone().isoformat(timespec='seconds')
    m = re.search(r'(?:^|&)v=([\w.\-]+)', zapytanie)
    skrypt_chrome['wersja'] = m.group(1) if m else 'starsza'


class Obsluga(BaseHTTPRequestHandler):
    DOZWOLONE_HOSTY = {f'127.0.0.1:{PORT}', f'localhost:{PORT}'}

    def _wyslij(self, kod, body, typ):
        self.send_response(kod)
        self.send_header('Content-Type', typ)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, kod, dane):
        self._wyslij(kod, json.dumps(dane, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def _host_ok(self):
        # Ochrona przed stronami internetowymi próbującymi rozmawiać z programem (DNS rebinding).
        return self.headers.get('Host', '') in self.DOZWOLONE_HOSTY

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {'blad': 'zabronione'})
        sciezka, _, zapytanie = self.path.partition('?')
        if sciezka == '/':
            self._wyslij(200, STRONA.encode(), 'text/html; charset=utf-8')
        elif sciezka == '/api/stan':
            self._json(200, stan_publiczny())
        elif sciezka == '/api/log':
            self._json(200, {'linie': ostatnie_linie_logu()})
        elif sciezka == '/status':   # dla skryptu w Chrome
            kontakt_skryptu(zapytanie)
            self._json(200, {'ok': True, **czuwanie.stan, 'wersja': WERSJA, 'skrypt_wersja': WERSJA_SKRYPTU})
        elif sciezka == '/zdarzenia':
            kontakt_skryptu(zapytanie)
            with blokada:
                zdarzenia = wczytaj_json(PLIK_STAN, {}).get('zdarzenia', [])[-50:]
            self._json(200, {'zdarzenia': zdarzenia})
        elif sciezka == '/pomocnik.user.js':
            try:
                with open(PLIK_SKRYPTU, 'rb') as f:
                    self._wyslij(200, f.read(), 'text/javascript; charset=utf-8')
            except FileNotFoundError:
                self._json(404, {'blad': 'brak pliku skryptu'})
        else:
            self._json(404, {'blad': 'nie ma'})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {'blad': 'zabronione'})
        # Tylko JSON i tylko z naszej strony – zwykła strona internetowa nie może wysłać takiego żądania.
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://{h}' for h in self.DOZWOLONE_HOSTY}:
            return self._json(403, {'blad': 'zabronione'})
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            return self._json(415, {'blad': 'wymagany JSON'})
        try:
            dl = min(int(self.headers.get('Content-Length') or 0), 1_000_000)
            dane = json.loads(self.rfile.read(dl) or b'{}')
        except ValueError:
            return self._json(400, {'blad': 'zły JSON'})
        if not isinstance(dane, dict):
            return self._json(400, {'blad': 'zły JSON'})
        sciezka = self.path.split('?')[0]

        try:
            if sciezka == '/api/ustawienia':
                haslo = dane.pop('haslo', '') or ''
                k = zapisz_konfig(dane)
                if haslo:
                    haslo_zapisz(k['login'], haslo)
                if 'autostart' in dane:
                    autostart_ustaw(bool(k['autostart']))
                if any(kl in dane for kl in ('login', 'imap_serwer', 'imap_port', 'foldery', 'nadawca',
                                             'akceptuj_przekazane', 'co_ile_sekund')) or haslo:
                    czuwanie.restart.set()
                return self._json(200, {'ok': True})

            if sciezka == '/api/sprawdz':
                k = uporzadkuj({**konfig(), **{kl: w for kl, w in dane.items() if kl in DOMYSLNE}})
                try:
                    m = polacz(k, dane.get('haslo') or None)
                except Exception as e:
                    return self._json(200, {'ok': False, 'komunikat': przyjazny_blad(e)})
                try:
                    liczba = sum(len(u) for u in policz_maile_z_muzeum(k, m).values())
                except Exception as e:
                    return self._json(200, {'ok': False, 'komunikat': 'Zalogowano, ale: ' + przyjazny_blad(e)})
                finally:
                    try:
                        m.logout()
                    except Exception:
                        pass
                return self._json(200, {'ok': True, 'komunikat': f'Połączono. Wiadomości z muzeum w skrzynce: {liczba}.'
                                        + ('' if liczba else ' To normalne, jeśli muzeum jeszcze nic nie przysłało na ten adres.')})

            if sciezka == '/api/otworz':
                # Formularz w Chrome; z identyfikatorem odrzucenia skrypt wypełni go tym terminem.
                id_zd = str(dane.get('id') or '')
                if id_zd and not re.fullmatch(r'[\w-]{1,64}', id_zd):
                    return self._json(400, {'ok': False, 'komunikat': 'Zły identyfikator.'})
                otworz_w_chrome(ADRES_FORMULARZA + (f'#pomocnik={id_zd}' if id_zd else ''))
                return self._json(200, {'ok': True})

            if sciezka == '/api/telefon-test':
                ok = wyslij_na_telefon({**konfig(), 'telefon': True}, 'Pomocnik rezerwacji – próba',
                                       'Powiadomienia działają. Tak będzie wyglądał alarm o odrzuceniu.', True)
                return self._json(200, {'ok': ok})

            if sciezka == '/api/dzwiek-test':
                zagraj_alarm({**konfig(), **{kl: dane[kl] for kl in ('dzwiek', 'glosnosc') if kl in dane}}, 1)
                return self._json(200, {'ok': True})

            if sciezka in ('/api/proba', '/test'):
                godzina = str(dane.get('godzina') or '12:00').strip().replace('.', ':')
                zd = {'typ': 'odrzucenie', 'id': 'proba-' + secrets.token_hex(4), 'message_id': '',
                      'data': str(dane.get('data') or ''), 'godzina': godzina.zfill(5),
                      'jezyk': str(dane.get('jezyk') or 'polski').strip(),
                      'rodzaj': str(dane.get('rodzaj') or 'Zwiedzanie ogólne 3,5 godz.').strip()}
                try:
                    datetime.strptime(zd['data'], '%Y-%m-%d')
                except ValueError:
                    return self._json(400, {'ok': False, 'komunikat': 'Podaj datę.'})
                if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', zd['godzina']):
                    return self._json(400, {'ok': False, 'komunikat': 'Podaj godzinę w formacie GG:MM, np. 12:00.'})
                zd['temat_maila'] = f'[PRÓBA] Odrzucenie zapytania o grupę - {zd["data"]} {zd["godzina"]} - {zd["jezyk"]}'
                threading.Thread(target=nowe_zdarzenie, args=(konfig(), zd), daemon=True).start()
                return self._json(200, {'ok': True, 'id': zd['id']})

            if sciezka == '/api/sprawdz-wersje':
                if not nowe_wersje.stan['mozliwe']:
                    return self._json(400, {'ok': False, 'komunikat': 'Sprawdzanie nowych wersji działa w wersji na Windows.'})
                nowe_wersje.teraz.set()
                return self._json(200, {'ok': True})

            if sciezka == '/api/zakoncz':
                self._json(200, {'ok': True})
                log('Zamknięto program ze strony ustawień.')
                threading.Timer(0.5, lambda: os._exit(0)).start()
                return

            if sciezka == '/api/odinstaluj':
                log('Odinstalowanie programu.')
                odinstaluj()
                self._json(200, {'ok': True})
                threading.Timer(0.5, lambda: os._exit(0)).start()
                return

            self._json(404, {'blad': 'nie ma'})
        except Exception as e:
            log('Błąd API:', e)
            self._json(500, {'ok': False, 'komunikat': str(e)})

    def log_message(self, *a):
        pass


# ---------- Uruchomienie ----------

class Serwer(ThreadingHTTPServer):
    # Na Windowsie SO_REUSEADDR pozwala drugiej kopii programu zająć ten sam port – wtedy dwie kopie
    # czuwałyby naraz i każdy alarm przychodziłby podwójnie. Tam port musi być na wyłączność.
    allow_reuse_address = SYSTEM != 'Windows'

    def server_bind(self):
        if SYSTEM == 'Windows':
            import socket
            self.socket.setsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_EXCLUSIVEADDRUSE', -5), 1)
        super().server_bind()


def juz_dziala():
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/stan', timeout=2) as r:
            return json.load(r).get('wersja') is not None
    except Exception:
        return False


def uruchom(w_tle):
    adres = f'http://127.0.0.1:{PORT}/'
    if zainstaluj_windows():
        return
    if juz_dziala():
        if not w_tle:
            webbrowser.open(adres)
        return
    if SYSTEM == 'Darwin' and os.environ.get('POMOCNIK_LAUNCHER') and not w_tle:
        # Kliknięcie ikony na Macu: czuwanie startuje jako osobny proces w tle, a ten się kończy.
        # Inaczej macOS uznaje aplikację za wciąż otwartą i kolejne kliknięcia nic nie robią.
        subprocess.Popen(polecenie_startowe(), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            time.sleep(0.25)
            if juz_dziala():
                break
        webbrowser.open(adres)
        return
    try:
        serwer = Serwer(('127.0.0.1', PORT), Obsluga)
    except OSError:
        # Druga kopia uruchomiona w tej samej chwili (np. autostart i kliknięcie ikony) – to nie błąd.
        for _ in range(20):
            if juz_dziala():
                if not w_tle:
                    webbrowser.open(adres)
                return
            time.sleep(0.25)
        log(f'Port {PORT} jest zajęty przez inny program – nie można uruchomić.')
        powiadom_system(NAZWA, f'Nie można uruchomić: port {PORT} jest zajęty przez inny program.')
        return
    k = konfig()
    if k['autostart']:
        autostart_ustaw(True)   # odświeża ścieżkę, gdyby program przeniesiono
    log(f'{NAZWA} {WERSJA} uruchomiony. Strona ustawień: {adres}')
    czuwanie.start()
    nowe_wersje.start()
    if not w_tle:
        threading.Timer(0.8, lambda: webbrowser.open(adres)).start()
    try:
        serwer.serve_forever()
    except KeyboardInterrupt:
        log('Zatrzymano.')


def tryb_sprawdz():
    k = konfig()
    try:
        m = polacz(k)
    except Exception as e:
        sys.exit('Nie udało się zalogować: ' + przyjazny_blad(e))
    print('Logowanie OK:', k['login'])
    kryterium = 'TEXT' if k['akceptuj_przekazane'] else 'FROM'
    for folder in k['foldery']:
        try:
            otworz_folder(m, folder)
        except Exception as e:
            print(f'\nFolder {folder}: {e}')
            continue
        _, dane = m.uid('search', None, kryterium, '"' + k['nadawca'] + '"')
        uidy = (dane[0] or b'').split()
        print(f'\nFolder {folder}: wiadomości z muzeum: {len(uidy)}. Ostatnie:')
        for uid in uidy[-10:]:
            _, d = m.uid('fetch', uid, '(BODY.PEEK[])')
            surowy = next((c[1] for c in d if isinstance(c, tuple)), None)
            try:
                zd = surowy and rozpoznaj(surowy, k['nadawca'], k['akceptuj_przekazane'])
            except Exception as e:
                print(f'  błąd odczytu maila {uid.decode()}: {e}')
                continue
            if zd and zd['typ'] == 'odrzucenie':
                print(f'  ODRZUCENIE  {zd["data"]} {zd["godzina"]} | {zd["jezyk"]} | {zd["rodzaj"] or "?"}')
            elif zd:
                print(f'  inny        {zd["temat_maila"]}')
    m.logout()


def tryb_test(arg):
    czesci = arg.split(maxsplit=2)
    if len(czesci) < 3:
        sys.exit('Użycie: pomocnik --test "2026-12-08 12:00 angielski"')
    dane = {'data': czesci[0], 'godzina': czesci[1], 'jezyk': czesci[2]}
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}/api/proba', data=json.dumps(dane).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        print(urllib.request.urlopen(req, timeout=5).read().decode())
    except OSError:
        sys.exit('Program nie działa – najpierw go uruchom.')


def main():
    if '--sprawdz' in sys.argv:
        return tryb_sprawdz()
    if '--test' in sys.argv:
        i = sys.argv.index('--test')
        return tryb_test(sys.argv[i + 1] if len(sys.argv) > i + 1 else '')
    uruchom('--w-tle' in sys.argv)


# ---------- Strona ustawień ----------

STRONA = r'''<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pomocnik rezerwacji</title>
<style>
  :root{
    --bg:#f5f5f3; --card:#ffffff; --text:#1d1d1b; --muted:#6b6b66; --line:#e3e2de;
    --accent:#1d1d1b; --accent-text:#ffffff; --gold:#a8843f;
    --ok:#2f7d4f; --ok-bg:#e7f3ec; --warn:#9a6b12; --warn-bg:#fbf1dc; --err:#b3372f; --err-bg:#fbe9e7;
    --input:#ffffff; --focus:#a8843f55;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#141413; --card:#1e1e1c; --text:#ecebe7; --muted:#a19f98; --line:#34332f;
      --accent:#ecebe7; --accent-text:#141413; --gold:#cfae6c;
      --ok:#6cc596; --ok-bg:#1d3327; --warn:#e2b458; --warn-bg:#3a2f18; --err:#f08a80; --err-bg:#3d1f1c;
      --input:#161615; --focus:#cfae6c55;
    }
  }
  *{box-sizing:border-box}
  [hidden]{display:none!important}
  body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  .wrap{max-width:880px;margin:0 auto;padding:32px 16px 64px}
  header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:24px;flex-wrap:wrap}
  h1{font-size:22px;font-weight:600;margin:0;letter-spacing:-.01em}
  h1 small{font-size:13px;color:var(--muted);font-weight:400;margin-left:8px}
  .pill{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;font-size:13px;font-weight:500;background:var(--card);border:1px solid var(--line)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
  .ok .dot{background:var(--ok);box-shadow:0 0 0 3px var(--ok-bg)}
  .warn .dot{background:var(--warn);box-shadow:0 0 0 3px var(--warn-bg)}
  .err .dot{background:var(--err);box-shadow:0 0 0 3px var(--err-bg)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 24px;margin-bottom:16px}
  .card h2{font-size:16px;font-weight:600;margin:0 0 4px;display:flex;align-items:center;gap:10px}
  .card h3{font-size:14px;font-weight:600;margin:18px 0 6px}
  .num{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;background:var(--accent);color:var(--accent-text);font-size:12px;font-weight:600}
  .lead{color:var(--muted);margin:0 0 16px;font-size:14px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px}
  .grid .full{grid-column:1/-1}
  @media (max-width:640px){.grid{grid-template-columns:1fr}.card{padding:18px 16px}}
  label{display:block;font-size:13px;font-weight:500;margin-bottom:5px}
  input[type=text],input[type=password],input[type=number],input[type=date],select{
    width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--input);color:var(--text);font:inherit;font-size:14px}
  input:focus,select:focus{outline:none;border-color:var(--gold);box-shadow:0 0 0 3px var(--focus)}
  input[type=range]{width:100%;accent-color:var(--gold)}
  .hint{font-size:12px;color:var(--muted);margin-top:4px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
  button,.btn{appearance:none;border:1px solid var(--line);background:var(--card);color:var(--text);padding:9px 16px;border-radius:8px;font:inherit;font-size:14px;font-weight:500;cursor:pointer;text-decoration:none;display:inline-block}
  button:hover,.btn:hover{border-color:var(--muted)}
  button.primary,.btn.primary{background:var(--accent);color:var(--accent-text);border-color:var(--accent)}
  button.danger{color:var(--err)}
  button:disabled{opacity:.5;cursor:default}
  .msg{font-size:13px;padding:8px 12px;border-radius:8px;display:none}
  .msg.ok{display:block;background:var(--ok-bg);color:var(--ok)}
  .msg.err{display:block;background:var(--err-bg);color:var(--err)}
  .msg.info{display:block;background:var(--bg);color:var(--muted)}
  .switch{display:flex;align-items:center;gap:12px;padding:12px 0;border-top:1px solid var(--line)}
  .switch .t{flex:1}
  .switch .t b{display:block;font-weight:500;font-size:14px}
  .switch .t span{font-size:13px;color:var(--muted)}
  .tgl{position:relative;width:42px;height:24px;flex:none;margin:0}
  .tgl input{opacity:0;width:0;height:0}
  .tgl i{position:absolute;inset:0;background:var(--line);border-radius:999px;transition:.15s;cursor:pointer}
  .tgl i:after{content:"";position:absolute;left:3px;top:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:.15s;box-shadow:0 1px 2px #0003}
  .tgl input:checked + i{background:var(--ok)}
  .tgl input:checked + i:after{transform:translateX(18px)}
  ol.steps{margin:0;padding-left:20px}
  ol.steps li{margin:7px 0}
  code{font:13px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--bg);padding:2px 6px;border-radius:5px;border:1px solid var(--line);word-break:break-all}
  .phone{display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap}
  .qr{background:#fff;padding:10px;border-radius:10px;border:1px solid var(--line);width:180px;height:180px;display:grid;place-items:center;flex:none;color:#6b6b66;font-size:12px;text-align:center}
  .tabs{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:4px 0 10px}
  .tabs button{border:0;border-radius:0;padding:6px 14px;font-size:13px}
  .tabs button.on{background:var(--accent);color:var(--accent-text)}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th{text-align:left;font-weight:500;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding:6px 8px;border-bottom:1px solid var(--line)}
  td{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  tr:last-child td{border-bottom:0}
  .tag{display:inline-block;font-size:12px;font-weight:500;padding:2px 8px;border-radius:999px;white-space:nowrap}
  .tag.err{background:var(--err-bg);color:var(--err)}
  .tag.info{background:var(--bg);color:var(--muted);border:1px solid var(--line)}
  .empty{color:var(--muted);font-size:14px;padding:8px 0}
  details summary{cursor:pointer;color:var(--muted);font-size:14px;margin-top:4px}
  details[open] summary{margin-bottom:12px}
  .table-wrap{overflow-x:auto}
  .checks{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:4px}
  @media (max-width:520px){.checks{grid-template-columns:1fr}}
  .check{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font-size:13px;color:inherit;text-decoration:none}
  a.check:hover{border-color:var(--muted)}
  .check span{color:var(--muted)}
  a{color:var(--gold)}
  .card{scroll-margin-top:16px}
  td .link{border:0;background:none;padding:0;color:var(--gold);font-size:13px;text-decoration:underline;cursor:pointer;white-space:nowrap}
  .check b{display:block;font-size:14px;font-weight:500}
  .check .dot{margin-top:6px}
  pre.log{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;max-height:260px;overflow:auto;white-space:pre-wrap;margin:0}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Pomocnik rezerwacji <small id="wersja"></small></h1>
    <div class="row" style="margin:0">
      <button id="otworz-formularz" title="Otwiera formularz rezerwacji w Chrome">Otwórz formularz rezerwacji</button>
      <span class="pill" id="stan"><span class="dot"></span><span id="stan-txt">Łączenie…</span></span>
    </div>
  </header>

  <section class="card">
    <h2>Stan</h2>
    <p class="lead" id="stan-opis">Program sprawdza skrzynkę co kilka sekund.</p>
    <div class="checks">
      <a class="check" id="c-poczta" href="#sekcja-poczta"><span class="dot"></span><div><b>Poczta</b><span></span></div></a>
      <a class="check" id="c-chrome" href="#sekcja-chrome"><span class="dot"></span><div><b>Skrypt w Chrome</b><span></span></div></a>
      <a class="check" id="c-telefon" href="#sekcja-telefon"><span class="dot"></span><div><b>Telefon</b><span></span></div></a>
      <a class="check" id="c-autostart" href="#sekcja-program"><span class="dot"></span><div><b>Autostart</b><span></span></div></a>
    </div>
    <h3>Ostatnie wiadomości z muzeum</h3>
    <div class="table-wrap"><table>
      <thead><tr><th>Odebrano</th><th>Rodzaj</th><th>Termin</th><th>Szczegóły</th><th></th></tr></thead>
      <tbody id="zdarzenia"></tbody>
    </table></div>
  </section>

  <section class="card" id="sekcja-poczta">
    <h2><span class="num">1</span>Skrzynka pocztowa</h2>
    <p class="lead">Skrzynka, na którą muzeum wysyła odpowiedzi na zapytania.</p>
    <form id="f-poczta" class="grid" autocomplete="off" onsubmit="return false">
      <div><label for="login">Adres e-mail</label><input type="text" id="login" placeholder="booking@firma.pl"></div>
      <div><label for="haslo">Hasło</label><input type="password" id="haslo" autocomplete="new-password">
        <div class="hint">Hasło jest przechowywane w zabezpieczonym magazynie systemu.</div></div>
      <div><label for="imap_serwer">Serwer IMAP</label><input type="text" id="imap_serwer" placeholder="imap.firma.pl"></div>
      <div><label for="imap_port">Port</label><input type="number" id="imap_port" value="993"></div>
      <details class="full"><summary>Ustawienia zaawansowane</summary>
        <div class="grid">
          <div><label for="foldery">Foldery do obserwowania</label><input type="text" id="foldery"><div class="hint">Oddzielone przecinkami, np. INBOX</div></div>
          <div><label for="nadawca">Nadawca wiadomości</label><input type="text" id="nadawca"></div>
          <div><label for="co_ile_sekund">Sprawdzaj co (sekund)</label><input type="number" id="co_ile_sekund" min="3"></div>
          <div class="switch" style="border:0;padding-top:24px"><div class="t"><b>Tryb testowy</b><span>Rozpoznawaj też maile przekazane dalej (Fwd)</span></div>
            <label class="tgl"><input type="checkbox" id="akceptuj_przekazane"><i></i></label></div>
        </div>
      </details>
    </form>
    <div class="row">
      <button class="primary" id="zapisz-poczta">Zapisz</button>
      <button id="sprawdz">Sprawdź połączenie</button>
    </div>
    <div class="row"><div class="msg" id="msg-poczta"></div></div>
  </section>

  <section class="card" id="sekcja-telefon">
    <h2><span class="num">2</span>Powiadomienia na telefon</h2>
    <p class="lead">Alarm na telefonie w chwili, gdy muzeum odrzuci zapytanie. Korzysta z darmowej aplikacji ntfy.</p>
    <div class="phone">
      <div class="qr" id="qr">Kod QR pojawi się po połączeniu z internetem</div>
      <div style="flex:1;min-width:260px">
        <div class="tabs"><button class="on" data-os="android">Android</button><button data-os="iphone">iPhone</button></div>
        <ol class="steps" data-os-pane="android">
          <li>Zainstaluj <a href="https://play.google.com/store/apps/details?id=io.heckel.ntfy" target="_blank" rel="noopener">ntfy ze Sklepu Google Play</a>.</li>
          <li>Otwórz ntfy, zezwól na powiadomienia i dotknij <b>+</b> w prawym dolnym rogu.</li>
          <li>Wpisz temat <code class="temat"></code> (albo zeskanuj kod QR aparatem), włącz <b>Instant delivery</b> i dotknij <b>Subscribe</b>.</li>
          <li>Zgódź się, gdy aplikacja zapyta o <b>wyłączenie optymalizacji baterii</b> – inaczej Android może opóźniać alarmy.</li>
          <li>Kliknij poniżej <b>Wyślij próbne powiadomienie</b> i sprawdź, czy dotarło.</li>
        </ol>
        <ol class="steps" data-os-pane="iphone" hidden>
          <li>Zainstaluj <a href="https://apps.apple.com/app/ntfy/id1625396347" target="_blank" rel="noopener">ntfy z App Store</a>.</li>
          <li>Otwórz ntfy, zezwól na powiadomienia i dotknij <b>+</b>.</li>
          <li>Wpisz temat <code class="temat"></code> i dotknij <b>Subscribe</b>.</li>
          <li>W <b>Ustawieniach iPhone’a → Powiadomienia → ntfy</b> włącz <b>Powiadomienia pilne</b> (Time Sensitive), żeby alarm przechodził przez tryb skupienia.</li>
          <li>Kliknij poniżej <b>Wyślij próbne powiadomienie</b> i sprawdź, czy dotarło.</li>
        </ol>
        <div class="row">
          <button id="telefon-test">Wyślij próbne powiadomienie</button>
        </div>
        <div class="row"><div class="msg" id="msg-telefon"></div></div>
      </div>
    </div>
    <details style="margin-top:14px"><summary>Jak ustawić głośny dźwięk alarmu na telefonie</summary>
      <div class="tabs"><button class="on" data-os="android">Android</button><button data-os="iphone">iPhone</button></div>
      <ol class="steps" data-os-pane="android">
        <li>W ntfy otwórz temat, dotknij <b>⋮ → Subscription settings</b> (Ustawienia subskrypcji).</li>
        <li>Włącz <b>Custom notification settings</b> i wybierz kanał <b>Max priority</b> – odrzucenia przychodzą z najwyższym priorytetem.</li>
        <li>Ustaw tam <b>dźwięk</b> (np. dzwonek alarmowy), <b>wibracje</b> i włącz <b>Zastąp tryb Nie przeszkadzać</b>.</li>
        <li>W głównych ustawieniach ntfy włącz <b>Keep alerting for highest priority</b> – telefon będzie dzwonił, dopóki nie odczytasz powiadomienia.</li>
        <li>Głośność alarmu to głośność <b>powiadomień</b> w telefonie – sprawdź, czy nie jest ściszona.</li>
      </ol>
      <ol class="steps" data-os-pane="iphone" hidden>
        <li>W <b>Ustawieniach → Powiadomienia → ntfy</b> włącz <b>Dźwięki</b> i <b>Powiadomienia pilne</b>.</li>
        <li>Głośność alarmu to głośność <b>dzwonka i alertów</b> (Ustawienia → Dźwięki i haptyka).</li>
        <li>Przełącznik wyciszenia z boku telefonu wycisza też alarm – przy czuwaniu zostaw go włączony.</li>
      </ol>
    </details>
    <div class="switch" style="margin-top:12px"><div class="t"><b>Powiadomienia na telefon</b><span>Wyłącz, jeśli nie korzystasz z aplikacji ntfy</span></div>
      <label class="tgl"><input type="checkbox" id="telefon" data-auto><i></i></label></div>
  </section>

  <section class="card">
    <h2><span class="num">3</span>Alarm na komputerze</h2>
    <p class="lead">Dźwięk odtwarzany na komputerze, gdy przyjdzie odrzucenie.</p>
    <div class="grid">
      <div><label for="dzwiek">Dźwięk</label><select id="dzwiek" data-auto-val></select></div>
      <div><label for="powtorzenia">Ile razy odtworzyć</label><select id="powtorzenia" data-auto-val>
        <option value="1">1 raz</option><option value="3">3 razy</option><option value="5">5 razy</option><option value="10">10 razy</option></select></div>
      <div class="full"><label for="glosnosc">Głośność: <span id="glosnosc-txt"></span></label><input type="range" id="glosnosc" min="10" max="100" step="5" data-auto-val></div>
    </div>
    <div class="row"><button id="dzwiek-test">Odtwórz</button><span class="hint">Głośność zależy też od głośności systemu.</span></div>
  </section>

  <section class="card" id="sekcja-chrome">
    <h2><span class="num">4</span>Przeglądarka Chrome</h2>
    <p class="lead">Skrypt w Chrome wypełnia formularz rezerwacji i prowadzi dziennik zgłoszeń.</p>
    <ol class="steps">
      <li>Zainstaluj rozszerzenie <a href="https://chromewebstore.google.com/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo" target="_blank" rel="noopener">Tampermonkey</a>.</li>
      <li>Wejdź w <code>chrome://extensions</code>, przy Tampermonkey kliknij <b>Szczegóły</b> i włącz <b>Zezwalaj na skrypty użytkownika</b>.</li>
      <li>Kliknij przycisk poniżej, a potem <b>Zainstaluj</b> (lub <b>Aktualizuj</b>) w oknie Tampermonkey.</li>
      <li>Otwórz <a href="https://visit.auschwitz.org/formularz.html" target="_blank" rel="noopener">formularz rezerwacji</a> – w prawym górnym rogu pojawi się panel. Gdy Tampermonkey zapyta o dostęp do <code>127.0.0.1</code>, wybierz <b>Zawsze zezwalaj</b>.</li>
    </ol>
    <div class="row"><a class="btn primary" href="/pomocnik.user.js">Zainstaluj skrypt w Chrome</a></div>
    <div class="switch" style="margin-top:16px"><div class="t"><b>Otwieraj formularz po odrzuceniu</b><span>Chrome otworzy się z formularzem wypełnionym tym samym terminem</span></div>
      <label class="tgl"><input type="checkbox" id="otwieraj_formularz" data-auto><i></i></label></div>
  </section>

  <section class="card" id="sekcja-program">
    <h2><span class="num">5</span>Próba i ustawienia programu</h2>
    <p class="lead">Próbny alarm działa tak jak prawdziwe odrzucenie. W formularzu kliknij wtedy „Pomiń”, a nie „Wyślij”.</p>
    <div class="grid">
      <div><label for="p-data">Data</label><input type="date" id="p-data"></div>
      <div><label for="p-godzina">Godzina</label><input type="text" id="p-godzina" value="12:00"></div>
      <div><label for="p-jezyk">Język</label><input type="text" id="p-jezyk" value="angielski"></div>
      <div><label for="p-rodzaj">Rodzaj zwiedzania</label><input type="text" id="p-rodzaj" value="Zwiedzanie ogólne 3,5 godz."></div>
    </div>
    <div class="row"><button id="proba">Uruchom próbny alarm</button><div class="msg" id="msg-proba"></div></div>
    <div style="margin-top:20px">
      <div class="switch"><div class="t"><b>Uruchamiaj przy starcie komputera</b><span>Program działa w tle, bez okna</span></div>
        <label class="tgl"><input type="checkbox" id="autostart" data-auto><i></i></label></div>
      <div class="switch"><div class="t"><b>Ostrzegaj o problemach z pocztą</b><span>Powiadomienie, gdy przez 10 minut nie da się sprawdzić skrzynki</span></div>
        <label class="tgl"><input type="checkbox" id="alarm_bez_polaczenia" data-auto><i></i></label></div>
      <div class="switch" id="wersja-box"><div class="t"><b>Powiadamiaj o nowych wersjach</b><span id="wersja-txt">Powiadomienie na telefonie i komputerze, gdy będzie nowa wersja do pobrania</span></div>
        <a class="btn primary" id="pobierz" target="_blank" rel="noopener" hidden>Pobierz</a>
        <button id="sprawdz-wersje">Sprawdź teraz</button>
        <label class="tgl"><input type="checkbox" id="powiadamiaj_o_wersji" data-auto><i></i></label></div>
      <div class="switch"><div class="t"><b>Zakończ program</b><span>Czuwanie zostanie wyłączone do następnego uruchomienia</span></div>
        <button class="danger" id="zakoncz">Zakończ</button></div>
      <div class="switch"><div class="t"><b>Odinstaluj</b><span id="odinstaluj-opis">Usuwa autostart, skróty, ustawienia i zapisane hasło</span></div>
        <button class="danger" id="odinstaluj">Odinstaluj</button></div>
    </div>
    <details id="log-box" style="margin-top:8px"><summary>Dziennik działania programu</summary><pre class="log" id="log">…</pre></details>
  </section>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>
const $ = id => document.getElementById(id);
const POLA = ['login','imap_serwer','imap_port','foldery','nadawca','co_ile_sekund','akceptuj_przekazane'];
let wczytano = false, qrTemat = '';

async function api(sciezka, dane) {
  try {
    const r = await fetch(sciezka, dane === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(dane)});
    return await r.json();
  } catch (e) {
    if (dane === undefined) throw e;   // odświeżanie stanu samo pokazuje, że program nie działa
    return {ok:false, komunikat:'Program nie odpowiada – uruchom Pomocnika rezerwacji ponownie.'};
  }
}
const dzisISO = (dni = 0) => { const d = new Date(); d.setDate(d.getDate() + dni);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; };
function msg(id, tekst, typ){ const el = $(id); el.textContent = tekst; el.className = 'msg ' + (typ || 'info'); }
const plData = iso => iso ? iso.split('-').reverse().join('.') : '';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function temu(iso){
  if (!iso) return null;
  const s = Math.round((Date.now() - Date.parse(iso)) / 1000);
  return s < 60 ? 'przed chwilą' : s < 3600 ? `${Math.round(s/60)} min temu` : new Date(iso).toLocaleString('pl-PL');
}
function check(id, klasa, opis){ const el = $(id); el.className = 'check ' + klasa; el.querySelector(':scope > div > span').textContent = opis; }

function formularzPoczty(){
  const d = {};
  for (const p of POLA) { const el = $(p); d[p] = el.type === 'checkbox' ? el.checked : el.value.trim(); }
  if ($('haslo').value) d.haslo = $('haslo').value;
  return d;
}

// Podpowiedź serwera IMAP na podstawie adresu e-mail.
$('login').addEventListener('change', () => {
  const dom = ($('login').value.split('@')[1] || '').toLowerCase();
  if (!dom || $('imap_serwer').value) return;
  $('imap_serwer').value = dom === 'gmail.com' ? 'imap.gmail.com' : 'imap.' + dom;
});

// Zakładki Android / iPhone (przełączają wszystkie instrukcje naraz).
document.querySelectorAll('[data-os]').forEach(b => b.onclick = () => {
  document.querySelectorAll('[data-os]').forEach(x => x.classList.toggle('on', x.dataset.os === b.dataset.os));
  document.querySelectorAll('[data-os-pane]').forEach(p => p.hidden = p.dataset.osPane !== b.dataset.os);
});

async function odswiez(){
  let s;
  try { s = await api('/api/stan'); } catch {
    pokazStan('err', 'Program nie działa', 'Uruchom Pomocnika rezerwacji ponownie.');
    return;
  }
  // Po aktualizacji programu wczytaj stronę od nowa – nowa wersja może mieć nowe opcje.
  if (window.WERSJA_STRONY && window.WERSJA_STRONY !== s.wersja) return location.reload();
  window.WERSJA_STRONY = s.wersja;
  $('wersja').textContent = 'wersja ' + s.wersja;
  const nw = s.nowa_wersja;
  $('wersja-box').hidden = !nw.mozliwe;
  if (nw.mozliwe) {
    $('wersja-txt').textContent = nw.nowsza
      ? `Dostępna wersja ${nw.najnowsza}. Kliknij „Pobierz” i uruchom pobrany plik – program zaktualizuje się, ustawienia zostaną.`
      : nw.blad ? 'Nie udało się sprawdzić nowej wersji: ' + nw.blad
      : nw.sprawdzono ? `Masz najnowszą wersję (sprawdzono ${temu(nw.sprawdzono)}).`
      : 'Powiadomienie na telefonie i komputerze, gdy będzie nowa wersja do pobrania.';
    $('pobierz').hidden = !nw.nowsza;
    $('pobierz').href = nw.adres;
    $('sprawdz-wersje').hidden = nw.nowsza;
    $('wersja').textContent += nw.nowsza ? ` · dostępna ${nw.najnowsza}` : '';
  }
  window.OS = s.system;
  const u = s.ustawienia;
  const opoznienie = s.ostatnie_sprawdzenie ? (Date.now() - Date.parse(s.ostatnie_sprawdzenie)) / 1000 : 0;
  if (s.polaczono && opoznienie > Math.max(120, u.co_ile_sekund * 10)) {
    pokazStan('warn', 'Sprawdzanie opóźnione', `Ostatnie sprawdzenie poczty: ${temu(s.ostatnie_sprawdzenie)}. Program ponawia połączenie.`);
    check('c-poczta', 'warn', `${u.login} · sprawdzono ${temu(s.ostatnie_sprawdzenie)}`);
  } else if (s.polaczono) {
    pokazStan('ok', 'Czuwa', 'Wszystko działa. Program reaguje na odrzucenia w ciągu kilku sekund.');
    check('c-poczta', 'ok', `${u.login} · sprawdzono ${temu(s.ostatnie_sprawdzenie)}`);
  } else if (!u.login || !s.ma_haslo) {
    pokazStan('warn', 'Wymaga konfiguracji', 'Uzupełnij ustawienia skrzynki pocztowej poniżej.');
    check('c-poczta', 'warn', 'Nie skonfigurowano');
  } else {
    pokazStan('err', 'Brak połączenia', s.blad || 'Łączenie ze skrzynką…');
    check('c-poczta', 'err', s.blad || 'Łączenie…');
  }
  const ch = s.skrypt_chrome && (Date.now() - Date.parse(s.skrypt_chrome)) < 36e5;
  const staryskrypt = ch && s.skrypt_wersja && s.skrypt_chrome_wersja !== s.skrypt_wersja;
  if (!s.chrome) check('c-chrome', 'err', 'Nie znaleziono Google Chrome – zainstaluj go, formularz otworzy się w innej przeglądarce bez skryptu');
  else if (staryskrypt) check('c-chrome', 'warn', `Nieaktualny skrypt – kliknij „Zainstaluj skrypt w Chrome” (wersja ${s.skrypt_wersja})`);
  else check('c-chrome', ch ? 'ok' : 'warn', ch ? `Połączony ${temu(s.skrypt_chrome)}` : 'Otwórz formularz rezerwacji w Chrome, aby sprawdzić');
  check('c-telefon', u.telefon ? 'ok' : 'warn', u.telefon ? 'Włączone (ntfy)' : 'Wyłączone');
  check('c-autostart', s.autostart ? 'ok' : 'warn', s.autostart ? 'Uruchamia się z systemem' : 'Wyłączony');

  if (!wczytano) {
    for (const p of POLA) { const el = $(p), v = u[p]; if (el.type === 'checkbox') el.checked = !!v; else el.value = Array.isArray(v) ? v.join(', ') : v; }
    for (const p of ['telefon','otwieraj_formularz','alarm_bez_polaczenia','powiadamiaj_o_wersji']) $(p).checked = !!u[p];
    $('autostart').checked = s.autostart;
    $('dzwiek').innerHTML = s.dzwieki.map(d => `<option>${esc(d)}</option>`).join('');
    $('dzwiek').value = u.dzwiek;
    $('powtorzenia').value = String(u.powtorzenia);
    if (!$('powtorzenia').value) $('powtorzenia').add(new Option(u.powtorzenia + ' razy', u.powtorzenia, true, true));
    $('glosnosc').value = u.glosnosc; $('glosnosc-txt').textContent = u.glosnosc + '%';
    wczytano = true;
  }
  $('haslo').placeholder = s.ma_haslo ? 'Zapisane – wpisz, aby zmienić' : '';
  document.querySelectorAll('.temat').forEach(el => el.textContent = u.ntfy_temat);
  if (qrTemat !== u.ntfy_temat && window.QRCode) {
    qrTemat = u.ntfy_temat; $('qr').innerHTML = '';
    new QRCode($('qr'), {text: u.ntfy_serwer.replace(/\/+$/, '') + '/' + qrTemat, width: 160, height: 160});
  }

  const rows = s.zdarzenia.map(z => z.typ === 'odrzucenie'
    ? `<tr><td>${new Date(z.czas).toLocaleString('pl-PL')}</td><td><span class="tag err">Odrzucenie</span></td><td>${plData(z.data)}, ${esc(z.godzina)}</td><td>${esc(z.jezyk)} · ${esc(z.rodzaj)}${String(z.id).startsWith('proba') ? ' <span class="tag info">próba</span>' : ''}</td>
       <td><button class="link" data-otworz="${esc(z.id)}" title="Otwiera formularz w Chrome wypełniony tym terminem">Wypełnij ponownie</button></td></tr>`
    : `<tr><td>${new Date(z.czas).toLocaleString('pl-PL')}</td><td><span class="tag info">Inna</span></td><td>${z.data ? plData(z.data) : '–'}</td><td>${esc(z.temat_maila)}</td><td></td></tr>`);
  const html = rows.join('') || '<tr><td colspan="5" class="empty">Brak wiadomości od uruchomienia programu.</td></tr>';
  if ($('zdarzenia').dataset.html !== html) { $('zdarzenia').innerHTML = html; $('zdarzenia').dataset.html = html; }

  if ($('log-box').open) {
    const l = await api('/api/log'); $('log').textContent = l.linie.join('') || 'Brak wpisów.';
  }
}
function pokazStan(klasa, tekst, opis){ $('stan').className = 'pill ' + klasa; $('stan-txt').textContent = tekst; $('stan-opis').textContent = opis; }

$('zdarzenia').onclick = async e => {
  const b = e.target.closest('[data-otworz]');
  if (!b) return;
  const r = await api('/api/otworz', {id: b.dataset.otworz});
  if (!r.ok) alert(r.komunikat || 'Nie udało się otworzyć formularza.');
};
$('otworz-formularz').onclick = async () => {
  const r = await api('/api/otworz', {});
  if (!r.ok) alert(r.komunikat || 'Nie udało się otworzyć formularza.');
};
$('zapisz-poczta').onclick = async () => {
  const r = await api('/api/ustawienia', formularzPoczty());
  $('haslo').value = '';
  msg('msg-poczta', r.ok ? 'Zapisano. Program łączy się ze skrzynką.' : (r.komunikat || 'Błąd zapisu.'), r.ok ? 'ok' : 'err');
  setTimeout(odswiez, 1500);
};
$('sprawdz').onclick = async () => {
  msg('msg-poczta', 'Łączenie…', 'info'); $('sprawdz').disabled = true;
  try { const r = await api('/api/sprawdz', formularzPoczty()); msg('msg-poczta', r.komunikat, r.ok ? 'ok' : 'err'); }
  finally { $('sprawdz').disabled = false; }
};
$('telefon-test').onclick = async () => {
  const r = await api('/api/telefon-test', {});
  msg('msg-telefon', r.ok ? 'Wysłano. Powiadomienie powinno pojawić się na telefonie w ciągu kilku sekund.' : (r.komunikat || 'Nie udało się wysłać – sprawdź połączenie z internetem.'), r.ok ? 'ok' : 'err');
};
document.querySelectorAll('[data-auto]').forEach(el => el.onchange = () => api('/api/ustawienia', {[el.id]: el.checked}).then(odswiez));
document.querySelectorAll('[data-auto-val]').forEach(el => el.onchange = async () => {
  const r = await api('/api/ustawienia', {[el.id]: el.type === 'range' || el.id === 'powtorzenia' ? Number(el.value) : el.value});
  if (!r.ok) alert(r.komunikat || 'Nie udało się zapisać ustawienia.');
});
$('glosnosc').oninput = () => $('glosnosc-txt').textContent = $('glosnosc').value + '%';
$('dzwiek-test').onclick = () => api('/api/dzwiek-test', {dzwiek: $('dzwiek').value, glosnosc: Number($('glosnosc').value)});
$('proba').onclick = async () => {
  const r = await api('/api/proba', {data: $('p-data').value, godzina: $('p-godzina').value, jezyk: $('p-jezyk').value, rodzaj: $('p-rodzaj').value});
  msg('msg-proba', r.ok ? 'Alarm uruchomiony.' : (r.komunikat || 'Błąd.'), r.ok ? 'ok' : 'err');
  setTimeout(odswiez, 1000);
};
$('sprawdz-wersje').onclick = async () => {
  const r = await api('/api/sprawdz-wersje', {});
  if (!r.ok) return alert(r.komunikat || 'Nie udało się.');
  $('wersja-txt').textContent = 'Sprawdzanie…';
  setTimeout(odswiez, 3000);
};
$('zakoncz').onclick = async () => {
  if (!confirm('Zakończyć program? Odrzucenia nie będą wykrywane, dopóki go ponownie nie uruchomisz.')) return;
  await api('/api/zakoncz', {});
  pokazStan('err', 'Zakończono', 'Program został zamknięty. Uruchom go ponownie, aby wznowić czuwanie.');
};
$('odinstaluj').onclick = async () => {
  if (!confirm('Odinstalować Pomocnika rezerwacji? Zostaną usunięte ustawienia, zapisane hasło, autostart i skróty.\n\nSkrypt w Chrome usuń osobno w panelu Tampermonkey.')) return;
  await api('/api/odinstaluj', {});
  pokazStan('err', 'Odinstalowano', window.OS === 'Darwin'
    ? 'Program został usunięty. Przenieś jeszcze aplikację Pomocnik rezerwacji do Kosza.'
    : 'Program został usunięty z komputera.');
};
$('log-box').addEventListener('toggle', odswiez);

$('p-data').value = dzisISO(30);
odswiez(); setInterval(odswiez, 3000);
</script>
</body>
</html>
'''


if __name__ == '__main__':
    main()
