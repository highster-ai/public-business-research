import sys,unittest,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from core import *
from pipeline import configure,make_run,preflight,render
class Safety(unittest.TestCase):
 def test_url_boundaries(self):
  for u in ['http://google.com','file:///etc/passwd','https://127.0.0.1/','https://api.example.com/','https://www.instagram.com/direct/inbox/','https://web.whatsapp.com','https://mail.google.com/','https://example.com/api/leads']:
   with self.assertRaises(ValueError,msg=u):safe_url(u,'read-site')
  safe_url('https://www.google.com/maps/search/bookshops','find-maps',False)
  with self.assertRaises(ValueError):safe_url('https://example.com/','read-google',False)
  with self.assertRaises(ValueError):safe_url('https://instagram.com/test/','find-ig',False)
 def test_paths(self):
  with self.assertRaises(ValueError):contained('/tmp/reader','/tmp/reader/../../etc/passwd')
 def test_schema_extra(self):
  x=read(ROOT/'schemas/query.json')
  with self.assertRaises(jsonschema.ValidationError):check('query',{'queries':['test'],'execute':'curl x'})
 def test_phone(self):
  self.assertEqual(normalize_phone('0812 3456 7890','ID'),'+6281234567890')
  with self.assertRaises(ValueError):normalize_phone('123','ID')
 def test_no_probe(self):
  import yaml
  spec=yaml.safe_load((ROOT/'example.yaml').read_text());spec['dm_probe']=True
  with self.assertRaises(jsonschema.ValidationError):check('run',spec)
 def test_wrong_resolver(self):
  r=resolution('b','owner','not_found',None,[],[],'No evidence');r['resolver']='supervisor'
  with self.assertRaises(ValueError):validate_resolution(r,[],'ID')
 def test_missing_provenance(self):
  r=resolution('b','owner','found','Bob',['fake'],[],'test',0.5)
  with self.assertRaises(ValueError):validate_resolution(r,[],'ID')
 def test_not_found_valid(self):validate_resolution(resolution('b','owner','not_found',None,[],[],'No evidence'),[],'ID')
 def test_bad_numeric(self):
  e={'evidence_id':'e','business_id':'b','reader':'read-google','source_kind':'google_maps'}
  for val in ['4.5',True,8]:
   r=resolution('b','google_rating','found',val,['e'],[],'test',.8)
   with self.assertRaises((ValueError,jsonschema.ValidationError)):validate_resolution(r,[e],'ID')
 def test_cross_business(self):
  e={'evidence_id':'e','business_id':'wrong'}
  r=resolution('b','owner','found','Bob',['e'],[],'test',.8)
  with self.assertRaises((ValueError,jsonschema.ValidationError)):validate_resolution(r,[e],'ID')
 def test_immutable(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'out.json';write(p,{'v':1},True)
   with self.assertRaises(ValueError):write(p,{'v':2},True)
 def test_forged_quote(self):
  with tempfile.TemporaryDirectory() as t:
   snap={'url':'https://example.com','retrieved_at':now(),'reader':'read-site','observation':'Actual name'}
   write(Path(t)/'raw/read-site/snapshots/s_test.json',snap)
   e={'evidence_id':'e','business_id':'b','source_url':snap['url'],'source_kind':'official_site','snippet':'Invented name','raw_text':'Invented name','retrieved_at':snap['retrieved_at'],'reader':'read-site','snapshot_id':'s_test','snapshot_sha256':digest(snap['observation']),'field':'business_name'}
   with self.assertRaises(ValueError):verify_evidence(t,e)
 def test_renderer_gate(self):
  with tempfile.TemporaryDirectory() as t:
   write(Path(t)/'canonical/leads.json',{'fake':'data'});write(Path(t)/'control/approval.json',{'approved':True,'canonical_sha256':'wrong'})
   with self.assertRaises(ValueError):render(t,'md')
if __name__=='__main__':unittest.main()
