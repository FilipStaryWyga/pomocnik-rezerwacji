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

WERSJA = '4.0'
NAZWA = 'Pomocnik rezerwacji'
PORT = int(os.environ.get('POMOCNIK_PORT', 47631))  # inny port tylko do testów
SYSTEM = platform.system()  # 'Darwin' | 'Windows' | 'Linux'
SERWIS_HASLA = 'auschwitz-pomocnik'
ADRES_FORMULARZA = 'https://visit.auschwitz.org/formularz.html'
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
}


def wczytaj_json(sciezka, domyslne):
    try:
        with open(sciezka, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return domyslne


def zapisz_json(sciezka, dane):
    tmp = sciezka + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(dane, f, ensure_ascii=False, indent=2)
    os.replace(tmp, sciezka)


def konfig():
    k = {**DOMYSLNE, **wczytaj_json(PLIK_KONFIG, {})}
    if not k['ntfy_temat']:
        k['ntfy_temat'] = 'pomocnik-' + secrets.token_hex(8)
        zapisz_json(PLIK_KONFIG, k)
    return k


def zapisz_konfig(zmiany):
    with blokada:
        k = konfig()
        for klucz, wartosc in zmiany.items():
            if klucz in DOMYSLNE:
                k[klucz] = wartosc
        k['imap_port'] = int(k['imap_port'] or 993)
        k['co_ile_sekund'] = max(3, int(k['co_ile_sekund'] or 5))
        k['glosnosc'] = min(100, max(0, int(k['glosnosc'])))
        k['powtorzenia'] = min(20, max(1, int(k['powtorzenia'])))
        if isinstance(k['foldery'], str):
            k['foldery'] = [f.strip() for f in k['foldery'].split(',') if f.strip()] or ['INBOX']
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
    czesc = msg.get_body(preferencelist=('plain', 'html'))
    if czesc is None:
        return ''
    tresc = czesc.get_content()
    if czesc.get_content_type() == 'text/html':
        tresc = re.sub(r'(?i)<br\s*/?>|</p>|</div>', '\n', tresc)
        tresc = html.unescape(re.sub(r'<[^>]+>', '', tresc))
    return tresc


def rozpoznaj(surowy, nadawca_filtr, przekazane=False):
    """Zwraca słownik zdarzenia albo None, jeśli to nie jest mail z muzeum."""
    msg = email.message_from_bytes(surowy, policy=email.policy.default)
    nadawca = str(msg.get('From', ''))
    if nadawca_filtr.lower() not in nadawca.lower():
        if not (przekazane and nadawca_filtr.lower() in tekst_maila(msg).lower()):
            return None
    temat = ' '.join(str(msg.get('Subject', '')).split())
    zd = {'temat_maila': temat, 'nadawca': nadawca, 'message_id': str(msg.get('Message-ID', ''))}

    m = TEMAT_ODRZUCENIA.search(temat)
    if not m:
        zd['typ'] = 'inny'
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
                           'priority': 5 if pilne else 3}).encode()
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
        tytul = f'Odrzucono {d}, {zd["godzina"]}'
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
    reaguj(k, zd)


# ---------- Czuwanie nad pocztą ----------

def przyjazny_blad(e):
    t = str(e)
    if 'AUTHENTICATIONFAILED' in t or 'Invalid credentials' in t or 'authentication failed' in t.lower():
        return 'Nieprawidłowy login lub hasło.'
    if 'Application-specific password' in t or 'app password' in t.lower():
        return 'Gmail wymaga hasła do aplikacji (myaccount.google.com/apppasswords).'
    if isinstance(e, TimeoutError) or 'timed out' in t:
        return 'Serwer poczty nie odpowiada. Sprawdź adres serwera i połączenie z internetem.'
    if 'nodename nor servname' in t or 'getaddrinfo' in t or 'Name or service not known' in t:
        return 'Nie znaleziono serwera poczty. Sprawdź adres serwera IMAP i połączenie z internetem.'
    return t


def polacz(k, haslo=None):
    haslo = haslo if haslo is not None else haslo_odczytaj(k['login'])
    if not haslo:
        raise imaplib.IMAP4.error('Brak zapisanego hasła do skrzynki.')
    m = imaplib.IMAP4_SSL(k['imap_serwer'], int(k['imap_port']), timeout=30)
    m.login(k['login'], haslo)
    return m


def sprawdz_folder(k, m, folder):
    typ, _ = m.select(f'"{folder}"', readonly=True)
    if typ != 'OK':
        raise imaplib.IMAP4.error(f'Nie można otworzyć folderu {folder}.')
    uidvalidity = m.untagged_responses.get('UIDVALIDITY', [b'0'])[-1].decode()
    klucz = f'{k["login"]}|{folder}|{uidvalidity}'

    with blokada:
        ostatni = wczytaj_json(PLIK_STAN, {}).get('ostatni_uid', {}).get(klucz)

    _, dane = m.uid('search', None, f'UID {(ostatni or 0) + 1}:*')
    uidy = [int(u) for u in (dane[0] or b'').split()]
    if ostatni is None:
        nowy = max(uidy, default=0)   # pierwsze uruchomienie: stare maile pomijamy, czuwamy od teraz
    else:
        nowy = ostatni
        for uid in sorted(u for u in uidy if u > ostatni):
            _, d = m.uid('fetch', str(uid), '(BODY.PEEK[])')  # PEEK = nie oznacza jako przeczytane
            surowy = next((c[1] for c in d if isinstance(c, tuple)), None)
            if surowy:
                zd = rozpoznaj(surowy, k['nadawca'], k['akceptuj_przekazane'])
                if zd:
                    zd['id'] = f'{uidvalidity}-{uid}'
                    nowe_zdarzenie(k, zd)
            nowy = uid

    if nowy != ostatni:
        with blokada:
            stan = wczytaj_json(PLIK_STAN, {})
            stan.setdefault('ostatni_uid', {})[klucz] = nowy
            zapisz_json(PLIK_STAN, stan)


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
skrypt_chrome = {'ostatnio': None}   # kiedy skrypt w Chrome ostatnio odezwał się do programu


# ---------- Lokalny serwer: strona ustawień + API dla skryptu w Chrome ----------

def stan_publiczny():
    k = konfig()
    with blokada:
        zdarzenia = wczytaj_json(PLIK_STAN, {}).get('zdarzenia', [])
    return {
        'wersja': WERSJA, 'system': SYSTEM,
        **czuwanie.stan,
        'ustawienia': {kl: k[kl] for kl in DOMYSLNE},
        'ma_haslo': bool(haslo_odczytaj(k['login'])),
        'autostart': autostart_wlaczony(),
        'dzwieki': list(dostepne_dzwieki()),
        'skrypt_chrome': skrypt_chrome['ostatnio'],
        'zdarzenia': list(reversed(zdarzenia[-30:])),
    }


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
        sciezka = self.path.split('?')[0]
        if sciezka == '/':
            self._wyslij(200, STRONA.encode(), 'text/html; charset=utf-8')
        elif sciezka == '/api/stan':
            self._json(200, stan_publiczny())
        elif sciezka == '/api/log':
            self._json(200, {'linie': ostatnie_linie_logu()})
        elif sciezka == '/status':   # dla skryptu w Chrome
            skrypt_chrome['ostatnio'] = datetime.now().astimezone().isoformat(timespec='seconds')
            self._json(200, {'ok': True, **czuwanie.stan})
        elif sciezka == '/zdarzenia':
            skrypt_chrome['ostatnio'] = datetime.now().astimezone().isoformat(timespec='seconds')
            with blokada:
                self._json(200, {'zdarzenia': wczytaj_json(PLIK_STAN, {}).get('zdarzenia', [])[-50:]})
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
        dl = int(self.headers.get('Content-Length', 0))
        try:
            dane = json.loads(self.rfile.read(dl) or b'{}')
        except json.JSONDecodeError:
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
                k = {**konfig(), **{kl: w for kl, w in dane.items() if kl in DOMYSLNE}}
                if isinstance(k['foldery'], str):
                    k['foldery'] = [f.strip() for f in k['foldery'].split(',') if f.strip()] or ['INBOX']
                try:
                    m = polacz(k, dane.get('haslo') or None)
                except Exception as e:
                    return self._json(200, {'ok': False, 'komunikat': przyjazny_blad(e)})
                try:
                    liczba = 0
                    for folder in k['foldery']:
                        m.select(f'"{folder}"', readonly=True)
                        kryterium = f'TEXT "{k["nadawca"]}"' if k['akceptuj_przekazane'] else f'FROM "{k["nadawca"]}"'
                        _, d = m.uid('search', None, kryterium)
                        liczba += len((d[0] or b'').split())
                finally:
                    m.logout()
                return self._json(200, {'ok': True, 'komunikat': f'Połączono. Wiadomości z muzeum w skrzynce: {liczba}.'})

            if sciezka == '/api/telefon-test':
                ok = wyslij_na_telefon({**konfig(), 'telefon': True}, 'Pomocnik rezerwacji – próba',
                                       'Powiadomienia działają. Tak będzie wyglądał alarm o odrzuceniu.', True)
                return self._json(200, {'ok': ok})

            if sciezka == '/api/dzwiek-test':
                zagraj_alarm({**konfig(), **{kl: dane[kl] for kl in ('dzwiek', 'glosnosc') if kl in dane}}, 1)
                return self._json(200, {'ok': True})

            if sciezka in ('/api/proba', '/test'):
                zd = {'typ': 'odrzucenie', 'id': 'proba-' + secrets.token_hex(4), 'message_id': '',
                      'data': dane.get('data'), 'godzina': dane.get('godzina', '12:00'),
                      'jezyk': dane.get('jezyk', 'polski'), 'rodzaj': dane.get('rodzaj', 'Zwiedzanie ogólne 3,5 godz.')}
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', zd['data'] or ''):
                    return self._json(400, {'ok': False, 'komunikat': 'Podaj datę.'})
                zd['temat_maila'] = f'[PRÓBA] Odrzucenie zapytania o grupę - {zd["data"]} {zd["godzina"]} - {zd["jezyk"]}'
                threading.Thread(target=nowe_zdarzenie, args=(konfig(), zd), daemon=True).start()
                return self._json(200, {'ok': True, 'id': zd['id']})

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
        serwer = ThreadingHTTPServer(('127.0.0.1', PORT), Obsluga)
    except OSError:
        log(f'Port {PORT} jest zajęty przez inny program – nie można uruchomić.')
        powiadom_system(NAZWA, f'Nie można uruchomić: port {PORT} jest zajęty przez inny program.')
        return
    k = konfig()
    if k['autostart']:
        autostart_ustaw(True)   # odświeża ścieżkę, gdyby program przeniesiono
    log(f'{NAZWA} {WERSJA} uruchomiony. Strona ustawień: {adres}')
    czuwanie.start()
    if not w_tle:
        threading.Timer(0.8, lambda: webbrowser.open(adres)).start()
    try:
        serwer.serve_forever()
    except KeyboardInterrupt:
        log('Zatrzymano.')


def tryb_sprawdz():
    k = konfig()
    m = polacz(k)
    print('Logowanie OK:', k['login'])
    for folder in k['foldery']:
        m.select(f'"{folder}"', readonly=True)
        kryterium = f'TEXT "{k["nadawca"]}"' if k['akceptuj_przekazane'] else f'FROM "{k["nadawca"]}"'
        _, dane = m.uid('search', None, kryterium)
        uidy = (dane[0] or b'').split()
        print(f'\nFolder {folder}: wiadomości z muzeum: {len(uidy)}. Ostatnie:')
        for uid in uidy[-10:]:
            _, d = m.uid('fetch', uid, '(BODY.PEEK[])')
            zd = rozpoznaj(next(c[1] for c in d if isinstance(c, tuple)), k['nadawca'], k['akceptuj_przekazane'])
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
  .check{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font-size:13px}
  .check b{display:block;font-size:14px;font-weight:500}
  .check .dot{margin-top:6px}
  pre.log{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;max-height:260px;overflow:auto;white-space:pre-wrap;margin:0}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Pomocnik rezerwacji <small id="wersja"></small></h1>
    <span class="pill" id="stan"><span class="dot"></span><span id="stan-txt">Łączenie…</span></span>
  </header>

  <section class="card">
    <h2>Stan</h2>
    <p class="lead" id="stan-opis">Program sprawdza skrzynkę co kilka sekund.</p>
    <div class="checks">
      <div class="check" id="c-poczta"><span class="dot"></span><div><b>Poczta</b><span></span></div></div>
      <div class="check" id="c-chrome"><span class="dot"></span><div><b>Skrypt w Chrome</b><span></span></div></div>
      <div class="check" id="c-telefon"><span class="dot"></span><div><b>Telefon</b><span></span></div></div>
      <div class="check" id="c-autostart"><span class="dot"></span><div><b>Autostart</b><span></span></div></div>
    </div>
    <h3>Ostatnie wiadomości z muzeum</h3>
    <div class="table-wrap"><table>
      <thead><tr><th>Odebrano</th><th>Rodzaj</th><th>Termin</th><th>Szczegóły</th></tr></thead>
      <tbody id="zdarzenia"></tbody>
    </table></div>
  </section>

  <section class="card">
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

  <section class="card">
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

  <section class="card">
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

  <section class="card">
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
  const r = await fetch(sciezka, dane === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(dane)});
  return r.json();
}
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
  $('wersja').textContent = 'wersja ' + s.wersja;
  window.OS = s.system;
  const u = s.ustawienia;
  if (s.polaczono) {
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
  check('c-chrome', ch ? 'ok' : 'warn', ch ? `Połączony ${temu(s.skrypt_chrome)}` : 'Otwórz formularz rezerwacji w Chrome, aby sprawdzić');
  check('c-telefon', u.telefon ? 'ok' : 'warn', u.telefon ? 'Włączone (ntfy)' : 'Wyłączone');
  check('c-autostart', s.autostart ? 'ok' : 'warn', s.autostart ? 'Uruchamia się z systemem' : 'Wyłączony');

  if (!wczytano) {
    for (const p of POLA) { const el = $(p), v = u[p]; if (el.type === 'checkbox') el.checked = !!v; else el.value = Array.isArray(v) ? v.join(', ') : v; }
    for (const p of ['telefon','otwieraj_formularz','alarm_bez_polaczenia']) $(p).checked = !!u[p];
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
    new QRCode($('qr'), {text: 'https://ntfy.sh/' + qrTemat, width: 160, height: 160});
  }

  const rows = s.zdarzenia.map(z => z.typ === 'odrzucenie'
    ? `<tr><td>${new Date(z.czas).toLocaleString('pl-PL')}</td><td><span class="tag err">Odrzucenie</span></td><td>${plData(z.data)}, ${esc(z.godzina)}</td><td>${esc(z.jezyk)} · ${esc(z.rodzaj)}${String(z.id).startsWith('proba') ? ' <span class="tag info">próba</span>' : ''}</td></tr>`
    : `<tr><td>${new Date(z.czas).toLocaleString('pl-PL')}</td><td><span class="tag info">Inna</span></td><td>–</td><td>${esc(z.temat_maila)}</td></tr>`);
  $('zdarzenia').innerHTML = rows.join('') || '<tr><td colspan="4" class="empty">Brak wiadomości od uruchomienia programu.</td></tr>';

  if ($('log-box').open) {
    const l = await api('/api/log'); $('log').textContent = l.linie.join('') || 'Brak wpisów.';
  }
}
function pokazStan(klasa, tekst, opis){ $('stan').className = 'pill ' + klasa; $('stan-txt').textContent = tekst; $('stan-opis').textContent = opis; }

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
  msg('msg-telefon', r.ok ? 'Wysłano. Powiadomienie powinno pojawić się na telefonie w ciągu kilku sekund.' : 'Nie udało się wysłać – sprawdź połączenie z internetem.', r.ok ? 'ok' : 'err');
};
document.querySelectorAll('[data-auto]').forEach(el => el.onchange = () => api('/api/ustawienia', {[el.id]: el.checked}).then(odswiez));
document.querySelectorAll('[data-auto-val]').forEach(el => el.onchange = () => api('/api/ustawienia', {[el.id]: el.type === 'range' || el.id === 'powtorzenia' ? Number(el.value) : el.value}));
$('glosnosc').oninput = () => $('glosnosc-txt').textContent = $('glosnosc').value + '%';
$('dzwiek-test').onclick = () => api('/api/dzwiek-test', {dzwiek: $('dzwiek').value, glosnosc: Number($('glosnosc').value)});
$('proba').onclick = async () => {
  const r = await api('/api/proba', {data: $('p-data').value, godzina: $('p-godzina').value, jezyk: $('p-jezyk').value, rodzaj: $('p-rodzaj').value});
  msg('msg-proba', r.ok ? 'Alarm uruchomiony.' : (r.komunikat || 'Błąd.'), r.ok ? 'ok' : 'err');
  setTimeout(odswiez, 1000);
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

const d = new Date(); d.setDate(d.getDate() + 30); $('p-data').value = d.toISOString().slice(0, 10);
odswiez(); setInterval(odswiez, 3000);
</script>
</body>
</html>
'''


if __name__ == '__main__':
    main()
