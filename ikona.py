#!/usr/bin/env python3
"""
Rysuje ikonę programu (bez zewnętrznych bibliotek): ciemny zaokrąglony kwadrat,
złoty pierścień i kropka – „czuwanie”.

  python3 ikona.py            → ikona.png (512 px) i ikona.ico (Windows)
"""
import struct
import zlib

TLO = (29, 29, 27)
ZLOTO = (201, 164, 92)


def pokrycie(x, y, n):
    """Kolor i przezroczystość punktu (x, y) w skali 0..1."""
    # zaokrąglony kwadrat z marginesem
    m, r = 0.06, 0.20
    ax, ay = abs(x - 0.5), abs(y - 0.5)
    h = 0.5 - m
    dx, dy = max(ax - (h - r), 0), max(ay - (h - r), 0)
    if ax > h or ay > h or dx * dx + dy * dy > r * r:
        return None
    d = ((x - 0.5) ** 2 + (y - 0.5) ** 2) ** 0.5
    if d < 0.085 or 0.19 < d < 0.255:
        return ZLOTO
    return TLO


def rysuj(rozmiar, probki=3):
    wiersze = []
    for py in range(rozmiar):
        wiersz = bytearray()
        for px in range(rozmiar):
            r = g = b = a = 0
            for sy in range(probki):
                for sx in range(probki):
                    c = pokrycie((px + (sx + .5) / probki) / rozmiar, (py + (sy + .5) / probki) / rozmiar, rozmiar)
                    if c:
                        r += c[0]; g += c[1]; b += c[2]; a += 1
            if a:
                wiersz += bytes((r // a, g // a, b // a, 255 * a // (probki * probki)))
            else:
                wiersz += b'\0\0\0\0'
        wiersze.append(bytes(wiersz))
    return wiersze


def png(wiersze):
    rozmiar = len(wiersze)
    surowe = b''.join(b'\0' + w for w in wiersze)
    blok = lambda typ, dane: struct.pack('>I', len(dane)) + typ + dane + struct.pack('>I', zlib.crc32(typ + dane))
    return (b'\x89PNG\r\n\x1a\n' + blok(b'IHDR', struct.pack('>IIBBBBB', rozmiar, rozmiar, 8, 6, 0, 0, 0))
            + blok(b'IDAT', zlib.compress(surowe, 9)) + blok(b'IEND', b''))


def ico(obrazy):
    """Plik .ico z obrazami PNG (obsługiwane od Windows Vista)."""
    naglowek = struct.pack('<HHH', 0, 1, len(obrazy))
    offset = 6 + 16 * len(obrazy)
    katalog, dane = b'', b''
    for rozmiar, p in obrazy:
        katalog += struct.pack('<BBBBHHII', rozmiar % 256, rozmiar % 256, 0, 0, 1, 32, len(p), offset + len(dane))
        dane += p
    return naglowek + katalog + dane


if __name__ == '__main__':
    with open('ikona.png', 'wb') as f:
        f.write(png(rysuj(512, 2)))
    with open('ikona.ico', 'wb') as f:
        f.write(ico([(s, png(rysuj(s, 4))) for s in (16, 24, 32, 48, 64, 256)]))
    print('Zapisano ikona.png i ikona.ico')
