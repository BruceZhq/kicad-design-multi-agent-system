import pytest
from ratsnestpro.repair.research import RepairResearch


def test_research_blocks_arbitrary_hosts_and_private_addresses(tmp_path, monkeypatch):
    research = RepairResearch(tmp_path)
    for url in ('http://docs.kicad.org', 'https://docs.kicad.org.evil.test', 'https://127.0.0.1'):
        with pytest.raises(ValueError):
            research.validate(url)
    from agents.ratsnestpro import web_tools
    monkeypatch.setattr(web_tools.socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ValueError):
        research.validate('https://docs.kicad.org/')


def test_search_is_cached_and_filters_untrusted_domains(tmp_path, monkeypatch):
    from agents.ratsnestpro import web_tools
    calls = []
    monkeypatch.setattr(web_tools, '_provider_search', lambda q: calls.append(q) or [
        {'href': 'https://docs.kicad.org/api', 'title': 'API'}, {'href': 'https://evil.test/a'}])
    research = RepairResearch(tmp_path)
    q = {'tool': 'web_search', 'query': 'pcbnew ToMM'}
    a = research.query(q)
    assert a == research.query(q) and len(calls) == 1
    assert len(a['results']) == 1
    assert 'untrusted' in a['authority']
