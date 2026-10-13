"""版本号必须只有一处真源：rpsboard.__version__ 与已安装包的 metadata 一致。"""

from __future__ import annotations

import importlib.metadata

import rpsboard


def test_package_version_matches_metadata():
    assert rpsboard.__version__ == importlib.metadata.version("rpsboard")


def test_api_reports_package_version():
    """API 文档里的版本不能是另一个写死的字符串（历史上 pyproject/package.json/
    FastAPI 三处各写一遍）。"""
    from rpsboard.api import create_app

    app = create_app()
    assert app.version == rpsboard.__version__
