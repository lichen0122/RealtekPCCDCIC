import agent


class _FakeResp:
    """模擬 requests.get 回傳物件:提供 .content(bytes)與 .raise_for_status()。"""
    def __init__(self, text, encoding='utf-8'):
        self.content = text.encode(encoding)

    def raise_for_status(self):
        pass


def _boom(*args, **kwargs):
    raise RuntimeError('network down')


def test_remote_success_writes_cache(tmp_path, monkeypatch):
    cache = tmp_path / 'ui_cache.html'
    bundled = tmp_path / 'bundled.html'
    bundled.write_text('<html>bundled</html>', encoding='utf-8')
    monkeypatch.setattr(agent.requests, 'get',
                        lambda *a, **k: _FakeResp('<html>remote</html>'))

    html, source = agent.load_web_ui_html('http://x/index.html', str(cache), str(bundled))

    assert source == 'remote'
    assert html == '<html>remote</html>'
    assert cache.read_text(encoding='utf-8') == '<html>remote</html>'


def test_remote_decodes_utf8_body(tmp_path, monkeypatch):
    cache = tmp_path / 'ui_cache.html'
    bundled = tmp_path / 'bundled.html'
    bundled.write_text('<html>bundled</html>', encoding='utf-8')
    chinese = '<html>選擇 project 路徑</html>'
    monkeypatch.setattr(agent.requests, 'get',
                        lambda *a, **k: _FakeResp(chinese))

    html, source = agent.load_web_ui_html('http://x/index.html', str(cache), str(bundled))

    assert source == 'remote'
    assert html == chinese
    assert cache.read_text(encoding='utf-8') == chinese


def test_remote_empty_body_falls_back_to_bundled(tmp_path, monkeypatch):
    cache = tmp_path / 'ui_cache.html'
    bundled = tmp_path / 'bundled.html'
    bundled.write_text('<html>bundled</html>', encoding='utf-8')
    monkeypatch.setattr(agent.requests, 'get', lambda *a, **k: _FakeResp(''))

    html, source = agent.load_web_ui_html('http://x/index.html', str(cache), str(bundled))

    assert source == 'bundled'
    assert html == '<html>bundled</html>'
    assert not cache.exists()   # 空的遠端內容不可污染快取


def test_remote_fail_uses_cache(tmp_path, monkeypatch):
    cache = tmp_path / 'ui_cache.html'
    cache.write_text('<html>cached</html>', encoding='utf-8')
    bundled = tmp_path / 'bundled.html'
    bundled.write_text('<html>bundled</html>', encoding='utf-8')
    monkeypatch.setattr(agent.requests, 'get', _boom)

    html, source = agent.load_web_ui_html('http://x/index.html', str(cache), str(bundled))

    assert source == 'cache'
    assert html == '<html>cached</html>'


def test_remote_and_cache_fail_uses_bundled(tmp_path, monkeypatch):
    cache = tmp_path / 'missing_cache.html'      # 不建立 -> 讀取失敗
    bundled = tmp_path / 'bundled.html'
    bundled.write_text('<html>bundled</html>', encoding='utf-8')
    monkeypatch.setattr(agent.requests, 'get', _boom)

    html, source = agent.load_web_ui_html('http://x/index.html', str(cache), str(bundled))

    assert source == 'bundled'
    assert html == '<html>bundled</html>'


def test_all_fail_returns_none(tmp_path, monkeypatch):
    cache = tmp_path / 'missing_cache.html'
    bundled = tmp_path / 'missing_bundled.html'
    monkeypatch.setattr(agent.requests, 'get', _boom)

    html, source = agent.load_web_ui_html('http://x/index.html', str(cache), str(bundled))

    assert source == 'none'
    assert html is None
