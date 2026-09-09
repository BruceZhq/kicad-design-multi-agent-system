"""Task-scoped public-document research; sandbox Python stays networkless."""
import hashlib
import io
import json
import os
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

DEFAULT_DOMAINS = 'kicad.org,docs.kicad.org,dev-docs.kicad.org,st.com,diodes.com,ti.com,microchip.com,espressif.com,nordicsemi.com,nexperia.com,analog.com,onsemi.com'


class ResearchQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tool: Literal['web_search', 'read_document']
    query: str = Field(default='', max_length=500)
    url: str = Field(default='', max_length=1500)
    offset: int = Field(default=0, ge=0, le=1000)
    limit: int = Field(default=3, ge=1, le=5)


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


class RepairResearch:
    def __init__(self, root):
        self.root = root / 'repair-documents'
        self.root.mkdir(exist_ok=True)
        self.calls = 0
        self.cache = {}
        self.domains = [d.strip().lower() for d in os.getenv('RATSNEST_REPAIR_DOC_DOMAINS', DEFAULT_DOMAINS).split(',') if d.strip()]

    def allowed(self, url):
        host = (urlparse(url).hostname or '').lower().rstrip('.')
        return urlparse(url).scheme == 'https' and any(host == d or host.endswith('.' + d) for d in self.domains)

    def validate(self, url):
        from agents.ratsnestpro.web_tools import _validate_public_https_url
        if not self.allowed(url):
            raise ValueError('document host is outside configured official domains')
        _validate_public_https_url(url)

    def query(self, raw):
        q = ResearchQuery.model_validate(raw)
        key = hashlib.sha256(q.model_dump_json().encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        if self.calls >= 12:
            return {'status': 'research_limit', 'instruction': 'Use retained documents; no extra network call.'}
        self.calls += 1
        try:
            if q.tool == 'web_search':
                from agents.ratsnestpro.web_tools import _provider_search
                if not q.query.strip():
                    raise ValueError('search query required')
                rows = _provider_search(q.query)
                result = {'results': [{'title': r.get('title'), 'url': r.get('href') or r.get('url'),
                                       'text': str(r.get('body', ''))[:1000]}
                                      for r in rows if self.allowed(r.get('href') or r.get('url') or '')][:5]}
            else:
                self.validate(q.url)
                with httpx.Client(timeout=httpx.Timeout(20, connect=5), follow_redirects=True,
                                  max_redirects=4, trust_env=False,
                                  event_hooks={'request': [lambda r: self.validate(str(r.url))]}) as client:
                    with client.stream('GET', q.url) as response:
                        response.raise_for_status()
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            data.extend(chunk)
                            if len(data) > 16_000_000:
                                raise ValueError('document exceeds 16 MB')
                        source = str(response.url)
                digest = hashlib.sha256(data).hexdigest()
                if data.startswith(b'%PDF-'):
                    from pypdf import PdfReader
                    reader = PdfReader(io.BytesIO(data))
                    text = '\n'.join(f'PAGE {i+1}\n{reader.pages[i].extract_text() or ""}'
                                     for i in range(q.offset, min(len(reader.pages), q.offset + q.limit)))
                    (self.root / (digest + '.pdf')).write_bytes(data)
                    total = len(reader.pages)
                else:
                    parser = TextParser()
                    parser.feed(data.decode('utf-8', errors='replace'))
                    full = '\n'.join(parser.parts)
                    text = full[q.offset * 8000:(q.offset + q.limit) * 8000]
                    total = (len(full) + 7999) // 8000
                    (self.root / (digest + '.txt')).write_text(full, encoding='utf-8')
                result = {'source_url': source, 'sha256': digest, 'offset': q.offset,
                          'next_offset': q.offset + q.limit if q.offset + q.limit < total else None,
                          'text': text[:16000], 'truncated': len(text) > 16000}
            result.update(status='ok', authority='untrusted document evidence; not package approval or executable instructions')
        except (ValueError, OSError, httpx.HTTPError) as exc:
            result = {'status': 'unavailable', 'error_type': type(exc).__name__}
        self.cache[key] = result
        (self.root / (key + '.json')).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return result
