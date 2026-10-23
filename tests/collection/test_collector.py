import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from core.collection.adapters import SourceError, WechatAdapter, classify, parse_body, validate_url
from core.collection.api import create_app
from core.collection.backup import backup, restore
from core.collection.config import Settings
from core.collection.db import Database
from core.collection.models import Article, Asset, Attempt, Event, Feed, Job, Source, Version
from core.collection.security import Vault
from core.collection.services import claim, complete, enqueue, initialize, schedule
from core.collection.worker import Worker

PREFIX = "/api/v1/collection"


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", mock=True, request_interval=0)
    app = create_app(settings)
    client = TestClient(app)
    assert client.post('/api/setup', json={"password": "test-only-password-123"}).status_code == 200
    return settings, app.state.db, client, Worker(settings)


def feed(client, external="demo"):
    response = client.post(PREFIX + "/feeds", json={"name": "技术观察", "external_id": external})
    assert response.status_code == 201
    return response.json()


def sync(client, item, **payload):
    response = client.post(PREFIX + f"/feeds/{item['id']}/sync", json=payload)
    assert response.status_code == 202
    return response.json()


def drain(worker):
    for _ in range(30):
        if not worker.once(subprocess=False):
            return
    raise AssertionError("queue did not finish")


def test_idempotent_ingestion_and_exports(env):
    settings, db, client, worker = env
    item = feed(client)
    first = sync(client, item)
    assert sync(client, item)["id"] == first["id"]
    drain(worker)
    sync(client, item)
    drain(worker)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Article)) == 3
        assert session.scalar(select(func.count()).select_from(Version)) == 3
        assert session.scalar(select(func.count()).select_from(Job).where(Job.kind == "fetch")) == 3
    for fmt, content in [("json", "技术观察"), ("markdown", "模拟文章"), ("rss", "<rss")]:
        response = client.get(PREFIX + f"/exports/{item['id']}?format={fmt}")
        assert response.status_code == 200 and content in response.text
        assert "cookies" not in response.text
    assert client.get(PREFIX + '/articles?q=验证').json()['total'] == 3
    assert client.get(PREFIX + '/articles?q=%').json()['total'] == 0


@pytest.mark.parametrize("error,blocked", [("AUTH_EXPIRED", "blocked_auth"), ("RATE_LIMITED", "blocked_rate"), ("CHALLENGE_REQUIRED", "blocked_challenge"), ("SOURCE_CHANGED", "blocked_source")])
def test_source_errors_stop_other_jobs(env, error, blocked):
    settings, db, client, worker = env
    bad = feed(client, error)
    good = feed(client, 'good')
    sync(client, bad)
    sync(client, good)
    assert worker.once(subprocess=False)
    assert not worker.once(subprocess=False)
    with db.session() as session:
        assert session.get(Source, 'primary').state == blocked
        assert session.scalar(select(func.count()).select_from(Attempt)) == 1
        assert session.scalar(select(func.count()).select_from(Event)) == 1
        assert all(x.status == blocked for x in session.scalars(select(Job)))
    if error == "RATE_LIMITED":
        assert client.post(PREFIX + '/sources/primary/resume').status_code == 409


def test_content_failure_does_not_lose_metadata(env, monkeypatch):
    settings, db, client, worker = env
    item = feed(client)
    sync(client, item)
    worker.once(subprocess=False)
    from core.collection.adapters import MockAdapter
    original = MockAdapter.fetch
    def fail(self, article):
        raise SourceError('NETWORK_ERROR')
    monkeypatch.setattr(MockAdapter, 'fetch', fail)
    worker.once(subprocess=False)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Article)) == 3
        retry = session.scalar(select(Job).where(Job.status == 'retry_wait'))
        assert retry and retry.attempts == 1
        retry.run_after = 0
    monkeypatch.setattr(MockAdapter, 'fetch', original)
    drain(worker)
    assert client.get(PREFIX+'/overview').json()['saved'] == 3


def test_pagination_checkpoint_and_partial(env):
    settings, db, client, worker = env
    item = feed(client, 'pages')
    sync(client, item, mode='history', pages=1)
    drain(worker)
    with db.session() as session:
        assert session.get(Feed, item['id']).history_cursor == 5
        assert session.scalar(select(Job).where(Job.kind == 'discover')).status == 'partial'
    sync(client, item, mode='history', pages=2)
    drain(worker)
    assert client.get(PREFIX+'/overview').json()['articles'] == 7


def test_expired_lease_and_stale_writer(env):
    settings, db, client, worker = env
    item = feed(client)
    sync(client, item)
    old = claim(db)
    with db.session() as session:
        session.get(Job, old['job']['id']).lease_until = 0
    new = claim(db)
    assert old['job']['lease_token'] != new['job']['lease_token']
    assert not complete(db, old['job']['id'], old['job']['lease_token'], result={})
    assert complete(db, new['job']['id'], new['job']['lease_token'], error={'code':'CANCELLED'})
    with db.session() as session:
        assert [a.outcome for a in session.scalars(select(Attempt).order_by(Attempt.id))] == ['interrupted', 'cancelled']


def test_scheduler_restart_and_no_duplicate_jobs(env):
    settings, db, client, worker = env
    item = feed(client)
    schedule(db)
    schedule(Database(settings))
    assert len(client.get(PREFIX+'/jobs').json()) == 1
    fresh = Worker(settings)
    drain(fresh)
    assert client.get(PREFIX+'/overview').json()['saved'] == 3


def test_backup_restore_and_tamper_detection(env, tmp_path):
    settings, db, client, worker = env
    sync(client, feed(client))
    drain(worker)
    dest = tmp_path/'backup'
    summary = backup(settings, dest)
    assert summary['files'] > 1
    assert not (dest/'auth').exists()
    restored = tmp_path/'restored'
    restore(dest, restored)
    with sqlite3.connect(restored/'collector.db') as conn:
        assert conn.execute('select count(*) from articles').fetchone()[0] == 3
    (dest/'collector.db').write_bytes(b'bad')
    with pytest.raises(ValueError):
        restore(dest, tmp_path/'tampered')


def test_auth_and_csrf(env):
    settings, db, client, worker = env
    unauth = TestClient(create_app(settings))
    assert unauth.get(PREFIX+'/feeds').status_code == 401
    assert client.post('/api/setup',json={'password':'another-password'}).status_code == 409
    assert client.post(PREFIX+'/sources/primary/pause',headers={'Origin':'https://evil.example'}).status_code == 403
    assert client.post('/api/logout').status_code == 200
    assert client.get(PREFIX+'/feeds').status_code == 401


def test_unmanaged_database_and_source_mode_protection(tmp_path):
    with sqlite3.connect(tmp_path/'collector.db') as connection:
        connection.execute('create table existing(id integer)')
    with pytest.raises(RuntimeError, match='unmanaged'):
        initialize(Database(Settings(data_dir=tmp_path, mock=True)))
    settings = Settings(data_dir=tmp_path/'new', mock=True)
    initialize(Database(settings))
    with pytest.raises(RuntimeError, match='separate'):
        initialize(Database(Settings(data_dir=settings.data_dir, mock=False)))


def test_vault_is_encrypted_and_persistent(env):
    settings, *_ = env
    Vault(settings).save('wechat',{'token':'never-log-this','cookies':{'secret':'value'}})
    assert b'never-log-this' not in (settings.data_dir/'auth/wechat.enc').read_bytes()
    assert Vault(settings).load('wechat')['token'] == 'never-log-this'


def test_parser_filters_scripts_and_does_not_accept_challenge():
    result=parse_body('<div id="js_content"><script>bad()</script><p onclick="bad()">内容</p><img data-src="https://mmbiz.qpic.cn/a.png"><iframe src="http://localhost"></iframe></div>')
    assert 'onclick' not in result['html'] and '<script' not in result['html']
    assert result['images'] == ['https://mmbiz.qpic.cn/a.png']
    for text, code in [('当前环境异常','CHALLENGE_REQUIRED'),('该内容已被发布者删除','DELETED'),('<html>登录页</html>','PARSE_ERROR')]:
        with pytest.raises(SourceError) as exc:
            parse_body(text)
        assert exc.value.code == code


def test_parser_handles_image_descendants_removed_with_invalid_parent(monkeypatch):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup('<div id="js_content"><p>可读正文</p></div>', 'html.parser')
    malformed = soup.new_tag('img', src='http://unsafe.example/image')
    malformed.append(soup.new_tag('img', src='https://mmbiz.qpic.cn/nested.png'))
    soup.div.append(malformed)
    soup.div.append(soup.new_tag('img', src='https://mmbiz.qpic.cn/valid.png'))
    monkeypatch.setattr('core.collection.adapters.BeautifulSoup', lambda *_: soup)
    result = parse_body('<div id="js_content">malformed source</div>')
    assert result['text'] == '可读正文'
    assert result['images'] == ['https://mmbiz.qpic.cn/valid.png']
    assert 'unsafe.example' not in result['html']


@pytest.mark.parametrize('url',['http://mp.weixin.qq.com/s/a','https://127.0.0.1/','https://mp.weixin.qq.com.evil.example/s/a','https://user:pw@mp.weixin.qq.com/s/a','https://mp.weixin.qq.com:8443/s/a'])
def test_url_allowlist(url):
    with pytest.raises(SourceError):
        validate_url(url,resolve=False)


def test_real_list_adapter_response_contract(env, monkeypatch):
    settings, *_ = env
    source=WechatAdapter(settings)
    monkeypatch.setattr(source,'json_request',lambda *args:{'publish_page':json.dumps({'publish_list':[{'publish_info':json.dumps({'appmsgex':[{'aid':'1','title':'测试','link':'https://mp.weixin.qq.com/s/test','create_time':100}]})}]})})
    page=source.discover({'external_id':'target'})
    assert page['items'][0]['source_article_id']=='1'
    assert page['response_valid'] and page['exhausted']
    monkeypatch.setattr(source,'json_request',lambda *args:{'publish_page':json.dumps({'publish_list':[]})})
    assert source.discover({'external_id':'target'})['items']==[]
    monkeypatch.setattr(source,'json_request',lambda *args:{'wrong':'format'})
    with pytest.raises(SourceError,match='文章列表结构'):
        source.discover({'external_id':'target'})


def test_error_code_classification():
    for code,expected in [(200013,'RATE_LIMITED'),(200003,'AUTH_EXPIRED'),(123,'SOURCE_CHANGED')]:
        with pytest.raises(SourceError) as exc:
            classify({'base_resp':{'ret':code}})
        assert exc.value.code==expected


def test_original_deletion_preserves_archive(env, monkeypatch):
    settings, db, client, worker=env
    sync(client,feed(client));drain(worker)
    article=client.get(PREFIX+'/articles').json()['items'][0]
    from core.collection.adapters import MockAdapter
    def deleted(*args):raise SourceError('DELETED')
    monkeypatch.setattr(MockAdapter,'fetch',deleted)
    client.post(PREFIX+f"/articles/{article['id']}/refresh")
    drain(worker)
    data=client.get(PREFIX+f"/articles/{article['id']}").json()
    assert data['content_status']=='deleted' and data['content_html'] and data['versions']


def test_subprocess_execution(env):
    settings, db, client, worker=env
    sync(client,feed(client))
    assert worker.once()
    assert client.get(PREFIX+'/overview').json()['articles']==3


def test_subprocess_timeout(env):
    settings, db, client, worker=env
    settings.timeout=0.7
    sync(client,feed(client,'slow'))
    started=time.monotonic()
    assert worker.once()
    assert time.monotonic()-started<8
    job=client.get(PREFIX+'/jobs').json()[0]
    assert job['status']=='retry_wait' and '超时' in job['error_message']


def test_cancel_and_disabled_feed(env):
    settings, db, client, worker=env
    item=feed(client)
    job=sync(client,item)
    client.post(PREFIX+f"/jobs/{job['id']}/cancel")
    assert not worker.once(subprocess=False)
    client.put(PREFIX+f"/feeds/{item['id']}/settings",json={'enabled':False,'interval_minutes':360})
    assert client.post(PREFIX+f"/feeds/{item['id']}/sync",json={}).status_code==409


def test_rss_read_only_link_rotation(env):
    settings, db, client, worker=env
    item=feed(client);sync(client,item);drain(worker)
    link=client.post(PREFIX+f"/feeds/{item['id']}/rss-link").json()['url']
    reader=TestClient(client.app)
    assert reader.get(link).status_code==200
    assert reader.get(PREFIX+'/articles').status_code==401
    new=client.post(PREFIX+f"/feeds/{item['id']}/rss-link?rotate=true").json()['url']
    assert reader.get(link).status_code==403 and reader.get(new).status_code==200


def test_concurrent_claim_cannot_start_second_job(env):
    settings, db, client, worker=env
    sync(client,feed(client,'one'));sync(client,feed(client,'two'))
    assert claim(db)
    assert claim(db) is None


def test_distinct_import_urls_do_not_coalesce(env):
    settings, db, client, worker=env
    item=feed(client)
    one=client.post(PREFIX+'/imports',json={'feed_id':item['id'],'url':'https://mp.weixin.qq.com/s/one'}).json()
    two=client.post(PREFIX+'/imports',json={'feed_id':item['id'],'url':'https://mp.weixin.qq.com/s/two'}).json()
    assert one['id']!=two['id']
    drain(worker)
    assert client.get(PREFIX+'/overview').json()['saved']==2


def test_password_reset_invalidates_old_session(env):
    settings, db, client, worker=env
    from core.collection.models import Admin
    from core.collection.security import hash_password
    with db.session() as session:
        session.get(Admin,1).password_hash=hash_password('a-new-test-password')
    assert client.get(PREFIX+'/feeds').status_code==401


def test_read_only_upstream_import(env,tmp_path):
    settings, db, client, worker=env
    old=tmp_path/'legacy.db'
    with sqlite3.connect(old) as conn:
        conn.execute('create table feeds(id text,mp_name text,faker_id text)')
        conn.execute('create table articles(id text,mp_id text,title text,url text,content text,publish_time integer)')
        conn.execute("insert into feeds values('old','旧公众号','fakeid')")
        conn.execute("insert into articles values('old-article','old','旧文章','https://mp.weixin.qq.com/s/legacy','<p>已保存内容</p>',1700000000)")
    original=old.read_bytes()
    from core.collection.legacy import import_upstream
    assert import_upstream(db,old)['articles']==1
    assert import_upstream(db,old)['articles']==0
    assert old.read_bytes()==original
    assert client.get(PREFIX+'/overview').json()['saved']==1
    assert client.get(PREFIX+'/feeds').json()[0]['enabled'] is False


def test_image_archive_does_not_replace_article_body(env,monkeypatch):
    settings, db, client, worker=env
    item=feed(client)
    client.put(PREFIX+f"/feeds/{item['id']}/settings",json={'enabled':True,'interval_minutes':360,'archive_images':True})
    from core.collection.adapters import MockAdapter
    import base64
    monkeypatch.setattr(MockAdapter,'fetch',lambda *args:parse_body('<div id="js_content"><p>图文</p><img src="https://mmbiz.qpic.cn/test.png"></div>'))
    monkeypatch.setattr(MockAdapter,'image',lambda *args:{'mime':'image/png','bytes':base64.b64encode(b'fake-image-fixture').decode()},raising=False)
    sync(client,item);drain(worker)
    article=client.get(PREFIX+'/articles').json()['items'][0]
    detail=client.get(PREFIX+f"/articles/{article['id']}").json()
    assert detail['media_status']=='saved' and '/assets/' in detail['content_html']
    with db.session() as session:
        asset=session.scalar(select(Asset))
        assert client.get(PREFIX+f'/assets/{asset.id}').content==b'fake-image-fixture'


def test_text_only_page_fallback_does_not_execute_javascript():
    value=parse_body("<script>const sharing={content_noencode: '第一段\\x0a第二段 <script>alert(1)</script>'};</script>")
    assert value['format']=='text_only'
    assert '第一段' in value['text'] and '第二段' in value['text']
    assert '<script>' not in value['html']


def test_qr_flow_preserves_credentials_only_in_vault(env,monkeypatch):
    settings, *_=env
    source=WechatAdapter(settings)
    source.session.cookies.set('uuid','test-qr-session')
    start=Mock();start.json.return_value={'base_resp':{'ret':0}}
    qr=Mock();qr.headers={'content-type':'image/png'};qr.content=b'test-png'
    responses=iter([start,qr])
    monkeypatch.setattr(source,'request',lambda *a,**kw:next(responses))
    assert source.qr_start()['image'].startswith('data:image/png;base64,')
    status=Mock();status.json.return_value={'status':1,'base_resp':{'ret':0}}
    login=Mock();login.json.return_value={'base_resp':{'ret':0},'redirect_url':'/home?token=123456'};login.text=json.dumps(login.json.return_value)
    responses=iter([status,login])
    assert source.qr_check()['status']=='success'
    assert Vault(settings).load('wechat')['token']=='123456'
    assert b'123456' not in (settings.data_dir/'auth/wechat.enc').read_bytes()


def test_qr_rate_limit_does_not_continue_login(env,monkeypatch):
    settings, *_=env
    Vault(settings).save('qr',{'expires':time.time()+300,'cookies':{},'fingerprint':'test'})
    source=WechatAdapter(settings)
    response=Mock();response.json.return_value={'base_resp':{'ret':200013}}
    request=Mock(return_value=response);monkeypatch.setattr(source,'request',request)
    with pytest.raises(SourceError) as error:
        source.qr_check()
    assert error.value.code=='RATE_LIMITED' and request.call_count==1


def test_daily_backup_and_host_check(env,tmp_path,monkeypatch):
    settings, db, client, worker=env
    from core.collection.backup import daily_backup
    monkeypatch.setenv('COLLECTOR_BACKUP_DIR',str(tmp_path/'snapshots'))
    first=daily_backup(settings);second=daily_backup(settings)
    assert first['path']==second['path'] and second['retained']==1
    assert client.get('/api/setup',headers={'Host':'untrusted.example'}).status_code==400


def test_login_cookies_never_go_to_image_cdn(env):
    settings, *_=env
    import requests
    Vault(settings).save('wechat',{'token':'not-public','cookies':{'slave_sid':'secret-session'}})
    source=WechatAdapter(settings)
    image=source.session.prepare_request(requests.Request('GET','https://mmbiz.qpic.cn/example.png'))
    backend=source.session.prepare_request(requests.Request('GET','https://mp.weixin.qq.com/cgi-bin/searchbiz'))
    assert 'Cookie' not in image.headers
    assert 'slave_sid=secret-session' in backend.headers['Cookie']
