import repolens


def test_package_exposes_version() -> None:
    assert repolens.__version__.count(".") == 2
