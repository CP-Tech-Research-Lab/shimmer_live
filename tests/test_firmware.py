from shimmer_live import firmware as fw


def test_versions_are_ranked():
    assert fw.assess("LogAndStream 0.11.0")[0] == fw.OLD
    assert fw.assess("LogAndStream 0.16.0")[0] == fw.OLD
    assert fw.assess("LogAndStream 1.0.5")[0] == fw.NEWER_EXISTS
    assert fw.assess(f"LogAndStream {fw.fmt(fw.LATEST_KNOWN)}")[0] == fw.CURRENT
    assert fw.assess("SDLog 0.22.0")[0] == fw.NOT_STREAMING
    assert fw.assess("")[0] == fw.UNKNOWN


def test_parse():
    assert fw.parse("LogAndStream 1.1.3") == ("LogAndStream", (1, 1, 3))
    assert fw.parse("garbage") is None
