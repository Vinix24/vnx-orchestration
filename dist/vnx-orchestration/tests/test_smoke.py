def test_vnx_core_import():
    import vnx_core
    assert vnx_core.__version__


def test_vnx_cli_import():
    from vnx_cli import __main__
    assert __main__.main


def test_vnx_cli_runs():
    from vnx_cli.__main__ import main
    rc = main([])
    assert rc == 0


def test_vnx_cli_version_flag():
    from vnx_cli.__main__ import main
    rc = main(["--version"])
    assert rc == 0
