"""Unavailable native list reads must not impersonate a successful empty list."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

@pytest.mark.parametrize('payload',[[],[{'id':0,'name':'Synthetic zero','items':2},{'id':'7','name':'Synthetic seven'}]])
def test_successful_list_response_preserves_empty_and_ids(monkeypatch,payload):
    import mdblist
    calls=[]
    class Reply:
        def raise_for_status(self):pass
        def json(self):return payload
    def get(url,**kwargs):
        assert url=='https://api.mdblist.com/lists/user' and kwargs['timeout']==15
        assert set(kwargs['params'])=={'apikey'}
        calls.append(url);return Reply()
    monkeypatch.setattr(mdblist.requests,'get',get)
    assert mdblist.get_user_lists('fixture-not-a-provider-key')==payload
    assert len(calls)==1

@pytest.mark.parametrize('payload',[None,{}, {'lists':[]},[None],[{'id':True,'name':'Invalid boolean ID'}],[{'id':1,'name':[]}]] )
def test_malformed_list_response_is_unavailable(monkeypatch,payload):
    import mdblist
    class Reply:
        def raise_for_status(self):pass
        def json(self):return payload
    monkeypatch.setattr(mdblist.requests,'get',lambda *a,**k:Reply())
    with pytest.raises(mdblist.MDBListUnavailable,match='MDBList lists unavailable'):
        mdblist.get_user_lists('fixture-not-a-provider-key')

@pytest.mark.parametrize('kind',['timeout','http','json'])
def test_transport_failure_is_typed_without_exposing_details(monkeypatch,kind):
    import mdblist,requests
    class Reply:
        def raise_for_status(self):
            if kind=='http':raise requests.HTTPError('synthetic upstream detail')
        def json(self):raise ValueError('synthetic upstream detail')
    calls=[]
    def get(*a,**k):
        calls.append(1)
        if kind=='timeout':raise requests.Timeout('synthetic upstream detail')
        return Reply()
    monkeypatch.setattr(mdblist.requests,'get',get)
    with pytest.raises(mdblist.MDBListUnavailable) as error:
        mdblist.get_user_lists('fixture-not-a-provider-key')
    assert str(error.value)=='MDBList lists unavailable' and len(calls)==1


def test_native_lists_route_keeps_auth_and_distinguishes_unavailable(tmp_path):
    script=r'''
import os,json,socket
from pathlib import Path
def deny(*a,**k):raise AssertionError('unexpected network in native read fixture')
socket.socket.connect=deny;socket.create_connection=deny;socket.getaddrinfo=deny
import app as native
import auth,db,mdblist,requests
native.app.config['TESTING']=True
uid=db.create_user('synthetic-list-reader','fixture$disabled')
c=native.app.test_client()
with c.session_transaction() as s:s.update(user_id=uid,user='synthetic-list-reader',role='user',auth_source='password')
path='/ui/api/mdblist/lists';calls=[];observations=[];mode='empty'
class Reply:
    def raise_for_status(self):
        if mode=='http':raise requests.HTTPError('synthetic upstream detail')
    def json(self):
        if mode=='json':raise ValueError('synthetic upstream detail')
        if mode=='shape':return {}
        if mode=='row':return [{'id':None,'name':[]}]
        return [] if mode=='empty' else [{'id':0,'name':'Synthetic zero'},{'id':'7','name':'Synthetic seven'}]
def get(url,**kwargs):
    assert url=='https://api.mdblist.com/lists/user' and kwargs['timeout']==15
    calls.append(mode)
    if mode=='timeout':raise requests.Timeout('synthetic upstream detail')
    return Reply()
mdblist.requests.get=get
assert native.app.test_client().get(path).status_code==401 and not calls
assert c.get(path).status_code==400 and not calls
# Disposable inert placeholders only, never provider credentials or a usable password.
db.update_user(uid,mdblist_api_key='fixture-not-a-provider-key',mdblist_list_ids='0,7')
for mode in ['empty','populated','timeout','http','json','shape','row']:
    response=c.get(path);body=response.json
    observations.append({'mode':mode,'HTTP':response.status_code,'body':body,'transport_calls':calls.count(mode)})
    if os.environ.get('MDBLIST_READ_EVIDENCE'):Path(os.environ['MDBLIST_READ_EVIDENCE']).write_text(json.dumps(observations,indent=2)+'\n')
    if mode in ['empty','populated']:
        assert response.status_code==200 and isinstance(body['lists'],list)
        if mode=='empty':assert body=={'lists':[]}
        else:assert body['lists']==[{'id':0,'name':'Synthetic zero'},{'id':'7','name':'Synthetic seven'}]
    else:assert response.status_code==503 and body=={'error':'MDBList lists unavailable'}
    assert 'synthetic upstream detail' not in response.get_data(as_text=True)
    assert db.get_user(uid)['mdblist_list_ids']=='0,7' and db.get_user(uid)['role']=='user'
assert len(calls)==7
before=len(calls);db.update_user(uid,enabled=0)
assert c.get(path).status_code==401 and len(calls)==before
print('NATIVE_MDBLIST_READ_BOUNDARY_PASS')
'''
    env={k:v for k,v in os.environ.items() if k in ['PATH','HOME','TMPDIR','PYTHONDONTWRITEBYTECODE','MDBLIST_READ_EVIDENCE']}
    env.update(DB_PATH=str(tmp_path/'native.sqlite'),AUTH_ENABLED='true',AUTH_SESSION_SECRET='synthetic-native-read-fixture-only',TORBOX_API_KEY='synthetic',MEDIA_PATH=str(tmp_path/'media'),SPORE_MEDIA_PATH=str(tmp_path/'spore'),LITE_MODE='true',CATCHUP_ENABLED='false',INSECURE_ALLOW_ANON='false',TMDB_API_KEY='',JELLYFIN_URL='',SEERR_URL='')
    result=subprocess.run([sys.executable,'-c',script],cwd=Path(__file__).resolve().parents[1],env=env,capture_output=True,text=True,timeout=45)
    assert result.returncode==0 and 'NATIVE_MDBLIST_READ_BOUNDARY_PASS' in result.stdout,result.stdout[-800:]+result.stderr[-1800:]
