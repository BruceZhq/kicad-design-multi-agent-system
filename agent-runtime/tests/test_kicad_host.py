"""kicad_host: the shared in-process KiCADInterface singleton."""

import pytest

from ratsnest import kicad_host
from ratsnest.config import Config


def test_get_host_raises_when_pcbnew_unavailable(monkeypatch):
    monkeypatch.setattr(kicad_host, "_host", None)
    monkeypatch.setattr(kicad_host, "bootstrap_kicad", lambda p: False)
    with pytest.raises(kicad_host.KicadHostError, match="pcbnew unavailable"):
        kicad_host.get_host(Config.load())


def test_get_host_raises_without_mcp_server_dir(monkeypatch):
    monkeypatch.setattr(kicad_host, "_host", None)
    monkeypatch.setattr(kicad_host, "bootstrap_kicad", lambda p: True)
    config = Config.load()
    config.mcp_server_dir = None
    with pytest.raises(kicad_host.KicadHostError, match="MCP-Server dir"):
        kicad_host.get_host(config)
