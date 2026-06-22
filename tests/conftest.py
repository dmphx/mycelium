"""
Gedeelde test-bootstrap voor de hele tests/ map.

pytest importeert deze conftest VOORDAT het ook maar een testmodule verzamelt.
Dat is het enige moment dat vroeg genoeg is: zodra de eerste testmodule
`import strm_generator` doet, binden de globals van strm_generator zich aan de
modules die op dat moment in sys.modules staan, en die binding ligt daarna vast
voor de hele sessie. Door hier de zware app-imports als MagicMock klaar te zetten,
krijgt strm_generator gegarandeerd de mocks.

Waarom dit nodig is (test-harness hygiene, geen productiebug):
Elke testmodule deed voorheen zelf `sys.modules.setdefault(naam, MagicMock())`.
setdefault is een no-op zodra `naam` al in sys.modules staat. test_parser.py
importeert het echte webhook_parser, dat via seerr/tmdb het ECHTE db en settings
in sys.modules laadt. pytest verzamelt alfabetisch, dus test_parser draait voor
test_strm_generator; de setdefault daar deed dan niets en strm_generator kreeg de
echte settings in plaats van de mock. Gevolg: settings.get("SPORE_ENABLED") las de
echte SQLite-laag in plaats van de in de test gezette return_value, zodat
_write_spore_stubs vroegtijdig terugkeerde en de stub .mkv/.minfo nooit aanmaakte.
Die twee tests faalden alleen in de volledige suite, niet in isolatie.

We gebruiken hier expres directe toewijzing (geen setdefault) zodat de mock altijd
wint, ongeacht of een eerdere import al een echt module achterliet.
"""
import os
import sys
from unittest.mock import MagicMock

# Verplichte env vars voor config.py (wordt NIET gemockt)
os.environ.setdefault("TORBOX_API_KEY", "test")
os.environ.setdefault("MEDIA_PATH", "/tmp/mycelium-test-media")
os.environ.setdefault("SPORE_MEDIA_PATH", "/tmp/mycelium-test-spore")
os.environ.setdefault("TORBOX_BASE_URL", "https://api.torbox.app/v1/api")
os.environ.setdefault("SPORE_ENABLED", "true")

# Repo-root op sys.path zodat strm_generator en co. importeerbaar zijn
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Zware imports forceren naar MagicMock zodat strm_generator zonder DB/netwerk
# laadt. Directe toewijzing (niet setdefault): de mock moet ook winnen als een
# eerder geimporteerde module het echte module al in sys.modules zette.
for _mod in ("db", "jellyfin", "settings", "torbox", "nfo_generator", "mp4_faststart"):
    sys.modules[_mod] = MagicMock()
