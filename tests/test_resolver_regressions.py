import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from core import *
from worker import resolve_model as resolve,strict_resolver_json,validate_resolver_batch

class LiveResolverRegression(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.f=read(Path(__file__).parent/'fixtures/acceptance-140831.json')
  cls.ev=cls.f['evidence'];cls.bs=cls.f['businesses'];cls.spec=cls.f['spec']
 def row(self,e,field,value,status='found',refs=None):
  return resolution(e['business_id'],field,status,value,refs if refs is not None else [e['evidence_id']],self.ev,'regression',.9 if status=='found' else 0)
 def valid_ig(self):
  rows=[]
  for b in self.bs:
   e=next(e for e in self.ev if e['business_id']==b['business_id'] and e['field']=='instagram')
   handle=e['snippet'].split('"')[1].split("'s")[0]
   rows.append(self.row(e,'instagram','https://www.instagram.com/'+handle+'/'))
  return {'resolutions':rows}
 def test_exact_malformed_json_retried(self):
  calls=[]
  def model(*args,**kw):
   calls.append(1)
   return strict_resolver_json(self.f['responses']['resolve-ig'][0] if len(calls)==1 else json.dumps(self.valid_ig()),kw['output_schema'])
  with tempfile.TemporaryDirectory() as run,patch('worker.llm',side_effect=model):
   out=resolve(run,'resolve-ig',self.f,self.spec)
  self.assertEqual(len(calls),2);self.assertEqual([r['evidence_ids'] for r in out['resolutions']],[r['evidence_ids'] for r in self.valid_ig()['resolutions']])
 def test_malformed_rejected_after_max_attempts(self):
  # Retry budget was raised from 2 to 3 attempts (dataquality fix); a
  # permanently malformed response must still fail closed, now after 3 tries.
  with tempfile.TemporaryDirectory() as run,patch('worker.llm',side_effect=json.JSONDecodeError('bad','{',1)) as m:
   with self.assertRaises(json.JSONDecodeError):resolve(run,'resolve-ig',self.f,self.spec)
   self.assertEqual(m.call_count,3)
 def test_exact_guessed_ids_rejected(self):
  rows=json.loads(self.f['responses']['resolve-ig'][1])
  with self.assertRaisesRegex(ValueError,'provenance'):validate_resolver_batch(rows,self.ev,self.bs,['instagram'],self.spec)
 def test_cross_business_and_nonexistent(self):
  for ref in [self.valid_ig()['resolutions'][1]['evidence_ids'][0],'e_nonexistent']:
   r=self.valid_ig()['resolutions'][0];r['evidence_ids']=[ref]
   with self.assertRaisesRegex(ValueError,'provenance'):validate_resolution(r,self.ev,'ID')
 def test_retry_does_not_bypass_provenance(self):
  # This fixture batch mixes rows citing genuinely real, correctly-provenanced
  # evidence with rows citing fabricated/guessed evidence_ids (missing the
  # 'e_' prefix real IDs always carry). The dataquality round-3 fix validates
  # and accepts each business/field row independently once it passes the
  # full, unmodified validate_resolution check, so one business' fabricated
  # citation can no longer sink a different business' correctly-evidenced
  # resolution in the same batch (the prior all-or-nothing batch validation
  # discarded every row, correct or not, whenever any single row failed).
  # Provenance itself must still never be bypassed: the fabricated-ID rows
  # must never be accepted no matter how many attempts, and the genuine rows
  # must still pass the exact same validate_resolution check as everything
  # else, with real evidence_ids and values traceable to real evidence.
  bad=json.loads(self.f['responses']['resolve-ig'][1])
  fabricated={'b_184f356c10a7b5fa','b_2f5b4f32deb7c06a','b_a5324b3c92eee088'}
  genuine={'b_8282d4742e4b0788':'https://www.instagram.com/svahaspamaimain/','b_f45af3db7cada6ac':'https://www.instagram.com/almaspabali/'}
  with tempfile.TemporaryDirectory() as run,patch('worker.llm',return_value=bad) as m:
   out=resolve(run,'resolve-ig',self.f,self.spec)
   self.assertEqual(m.call_count,3)
  seen=set()
  for r in out['resolutions']:
   validate_resolution(r,self.ev,'ID');seen.add(r['business_id'])
   if r['business_id'] in fabricated:
    self.assertEqual(r['status'],'ambiguous');self.assertIsNone(r['value'])
   else:
    self.assertEqual(r['status'],'found');self.assertEqual(r['value'],genuine[r['business_id']])
  self.assertEqual(seen,fabricated|set(genuine))
 def test_valid_instagram_ids_preserved(self):
  x=self.valid_ig();original=copy.deepcopy(x)
  validate_resolver_batch(x,self.ev,self.bs,['instagram'],self.spec)
  for a,b in zip(x['resolutions'],original['resolutions']):self.assertEqual(a['evidence_ids'],b['evidence_ids'])
 def test_exact_cta_not_phone(self):
  e=next(e for e in self.ev if 'MESSAGE ON WHATSAPP' in e['snippet'])
  self.assertEqual(contact_values(e,'whatsapp','ID'),set())
  with self.assertRaises(ValueError):validate_resolution(self.row(e,'whatsapp','MESSAGE ON WHATSAPP'),self.ev,'ID')
 def test_public_phone_plus_cta_not_whatsapp(self):
  e=next(e for e in self.ev if e['field']=='public_contact');cta=next(x for x in self.ev if 'MESSAGE ON WHATSAPP' in x['snippet'])
  r=self.row(e,'whatsapp','+6281911682025',refs=[e['evidence_id'],cta['evidence_id']])
  with self.assertRaisesRegex(ValueError,'proof of WhatsApp'):validate_resolution(r,self.ev,'ID')
 def test_exact_priority_failure(self):
  rows=json.loads(self.f['responses']['resolve-wa'][1])['resolutions']
  r=next(r for r in rows if r['field']=='public_contact');r['value']=normalize_phone(r['value'],'ID')
  with self.assertRaisesRegex(ValueError,'higher-priority'):validate_resolution(r,self.ev,'ID')
 def test_high_priority_wa_cannot_be_lost(self):
  e=next(e for e in self.ev if 'WA number:' in e['snippet'])
  self.assertEqual(contact_values(e,'whatsapp','ID'),{'+6281139405010'})
  validate_resolution(self.row(e,'whatsapp','+6281139405010'),self.ev,'ID')
  for refs in [[],[e['evidence_id']]]:
   with self.assertRaisesRegex(ValueError,'higher-priority'):validate_resolution(self.row(e,'whatsapp',None,'not_found',refs),self.ev,'ID')
  low={**e,'source_kind':'search_snippet','evidence_id':'e_lower','snippet':'WhatsApp +62 812 3733 9353'}
  with self.assertRaisesRegex(ValueError,'higher-priority'):validate_resolution(self.row(low,'whatsapp','+6281237339353'),self.ev+[low],'ID')
 def test_phone_normalization_and_targets(self):
  for v in ['0811-3940-5010','6281139405010','+62 811 39405010']:self.assertEqual(normalize_phone(v,'ID'),'+6281139405010')
  for s in ['https://wa.me/6281139405010','https://api.whatsapp.com/send?phone=6281139405010']:
   self.assertEqual(contact_values({'snippet':s},'whatsapp','ID'),{'+6281139405010'})
 def test_strict_json_duplicate_keys(self):
  with self.assertRaises(ValueError):strict_resolver_json('{"resolutions":[],"resolutions":[]}',read(ROOT/'schemas/resolver_output.json'))

if __name__=='__main__':unittest.main()
