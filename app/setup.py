from pathlib import Path
import json, os, yaml, shutil, subprocess, hashlib
from limits import MAX_COUNT
from charters import ROLES as roles, READERS as readers, role_instruction
ROOT=Path(os.environ.get('LEADGEN_ROOT', Path(__file__).resolve().parents[1])).resolve()
H=Path(os.environ.get('HERMES_HOME', Path.home()/'.hermes')).resolve()
fields=['business_name','instagram','whatsapp','public_contact','google_rating','google_review_count','dm_proxy','dm_measured','owner']
def obj(props, required=None):return {'type':'object','properties':props,'required':list(props) if required is None else required,'additionalProperties':False}
def arr(item):return {'type':'array','items':item}
s={'type':'string'}; nullable={'type':['string','null']}; stamp={'type':'string','format':'date-time'}
schemas={}
schemas['run']=obj({'category':s,'area':s,'country':{'type':'string','pattern':'^[A-Z]{2}$'},'count':{'type':'integer','minimum':1,'maximum':MAX_COUNT},'sources':{'type':'array','minItems':1,'uniqueItems':True,'items':{'enum':['google_maps','web','instagram']}},'requested_fields':{'type':'array','minItems':1,'uniqueItems':True,'items':{'enum':fields}},'dm_probe':{'const':False},'formats':{'type':'array','minItems':1,'uniqueItems':True,'items':{'enum':['md','xlsx','pdf']}}})
schemas['business']=obj({'business_id':s,'name_raw':s,'url':s,'evidence_ids':arr(s)})
schemas['evidence']=obj({'evidence_id':s,'business_id':s,'source_url':{'type':'string','format':'uri'},'source_kind':{'enum':['official_site','google_maps','instagram','public_source','search_snippet']},'snippet':s,'raw_text':s,'retrieved_at':stamp,'reader':{'enum':readers},'snapshot_id':s,'snapshot_sha256':{'type':'string','pattern':'^[a-f0-9]{64}$'},'field':{'enum':fields+['candidate','website','instagram_candidate','owner_reply','posting_recency']}})
schemas['reader_output']=obj({'observations':arr(obj({'business_id':s,'snapshot_id':s,'source_kind':schemas['evidence']['properties']['source_kind'],'field':schemas['evidence']['properties']['field'],'snippet':s})), 'candidates':arr(obj({'name_raw':s,'url':s,'snapshot_id':s,'snippet':s}))})
schemas['query']=obj({'queries':{'type':'array','minItems':1,'maxItems':15,'items':s}})
schemas['resolution']=obj({'business_id':s,'field':{'enum':fields},'status':{'enum':['found','not_found','ambiguous','not_public','error']},'value':{'type':['string','number','integer','object','null']},'confidence':{'type':'number','minimum':0,'maximum':1},'evidence_ids':{'type':'array','uniqueItems':True,'items':s},'resolver':s,'retrieved_at':stamp,'resolved_at':stamp,'method':s})
schemas['resolution']['allOf']=[{'if':{'properties':{'status':{'const':'found'}}},'then':{'properties':{'value':{'not':{'type':'null'}},'evidence_ids':{'minItems':1}}},'else':{'properties':{'value':{'type':'null'}}}}]
schemas['resolver_output']=obj({'resolutions':arr(schemas['resolution'])})
schemas['validation']=obj({'valid':{'type':'boolean'},'errors':arr(s),'checked_at':stamp})
schemas['audit']=obj({'at':stamp,'role':s,'event':s,'payload':{'type':'object'},'previous':s,'sha256':s})
for name,sch in schemas.items():
 sch['$schema']='https://json-schema.org/draft/2020-12/schema'
 (ROOT/'schemas'/f'{name}.json').write_text(json.dumps(sch,indent=2))
model=yaml.safe_load((H/'config.yaml').read_text())['model']
manifest={}
for role in roles:
 name='lg-'+role; p=H/'profiles'/name
 if p.exists():raise SystemExit(f'Refusing existing profile {p}')
 subprocess.run(['hermes','profile','create',name,'--no-alias','--no-skills','--description',f'Leadgen isolated {role}; run through {ROOT/"leadgen"} only.'],check=True,capture_output=True)
 cfg={'model':model,'toolsets':[],'platform_toolsets':{'cli':[]},'agent':{'max_turns':35},'browser':{'backend':'local','use_real_profile':False,'headed':True,'restrict_evaluate':True,'extension_control':{'enabled':False}},'kanban':{'auto_decompose':False,'max_in_progress':4,'max_in_progress_per_profile':1,'failure_limit':2},'memory':{'memory_enabled':False,'user_profile_enabled':False},'terminal':{'backend':'local','cwd':str(ROOT)},'mcp_servers':{}}
 (p/'config.yaml').write_text(yaml.safe_dump(cfg));(p/'config.yaml').chmod(0o600)
 skill=p/'skills'/('leads-report-md' if role=='render-md' else 'leads-'+role);skill.mkdir(parents=True,exist_ok=True)
 text=role_instruction(role)
 (skill/'SKILL.md').write_text(text)
 (p/'SOUL.md').write_text(f'You are the isolated leadgen {role}. Use only the pinned charter and supplied inputs. Refuse other work.')
 if role in ['render-xlsx','render-pdf']:
  fmt=role.split('-')[1];shutil.copytree(H/'skills/productivity'/fmt,p/'skills'/fmt)
 manifest[role]={'profile':name,'charter':str(skill/'SKILL.md'),'tools':['leadgen_open','leadgen_scroll'] if role in readers else [],'model':model,'enabled':not role.startswith('probe-')}
(ROOT/'profiles.json').write_text(json.dumps(manifest,indent=2))
(ROOT/'example.yaml').write_text(yaml.safe_dump({'category':'bookshops','area':'Ubud, Bali','country':'ID','count':3,'sources':['google_maps','web','instagram'],'requested_fields':fields,'dm_probe':False,'formats':['md','xlsx','pdf']},sort_keys=False))
(ROOT/'backups/default-config.sha256').write_text(hashlib.sha256((H/'config.yaml').read_bytes()).hexdigest())
print('Created',len(roles),'isolated profiles; default config unchanged')
