"""Orkestra: çok-ajanlı orkestratör."""

import sys

__version__ = "0.4.0"

# Bu dosya eski Python sürümlerinde de çalışabilecek sözdizimiyle yazılmıştır: yanlış sürümle
# çalıştırıldığında anlaşılmaz bir hata yerine ne yapılacağını söyleyen bir mesaj gösterilir.
if sys.version_info < (3, 11):
    _v = "%d.%d" % (sys.version_info[0], sys.version_info[1])
    raise SystemExit(
        "Orkestra Python 3.11 veya üstünü gerektirir; şu an Python " + _v + " çalışıyor ("
        + sys.executable + ").\n"
        "Windows'ta Orkestra klasöründe '.\\orkestra.cmd' yaz (doğru Python'u kendisi bulur) veya\n"
        "'py --list' ile kurulu sürümlere bakıp örneğin 'py -3.13 -m orkestra' kullan.\n"
        "Python 3.11+ yoksa: https://www.python.org/downloads/"
    )
