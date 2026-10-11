import sys,os
from pathlib import Path
sys.path.insert(0, os.environ.get('LEADGEN_HERMES_ROOT', str(Path.home()/'.hermes'/'hermes-agent')))
from browser import Browser
from guard import Guard
b=Browser(sys.argv[1],'find-maps')
with Guard(sys.argv[1],'find-maps',os.environ['HERMES_HOME'],True):
 result=b.open({'url':'https://www.google.com/maps/search/bookshop+Ubud+Bali'})
 print(result[:700])
b.close()
