def test_v2_health(client):
    r=client.get('/api/v2/platform/health')
    assert r.status_code==200
    assert 'summary' in r.get_json()

def test_v2_live(client):
    r=client.get('/api/v2/live?season=2025')
    assert r.status_code==200
    assert 'games' in r.get_json()

def test_live_page(client):
    r=client.get('/live')
    assert r.status_code==200
