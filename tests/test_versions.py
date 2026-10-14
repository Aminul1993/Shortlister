from fdm.versions import described_screenshot, proposals, save_proposal, save_screenshot, screenshots


def test_versions_are_numbered_and_never_overwritten(tmp_path):
    first = save_screenshot(tmp_path, "shop-1", b"one", {"model": "m1"})
    second = save_screenshot(tmp_path, "shop-1", b"two", {"model": "m2"})
    assert (first.number, first.path.name) == (1, "shop-1.png")
    assert (second.number, second.path.name) == (2, "shop-1-v2.png")
    saved = screenshots(tmp_path, "shop-1")
    assert [(v.number, v.path.read_bytes(), v.meta) for v in saved] == [(1, b"one", {"model": "m1"}),
                                                                        (2, b"two", {"model": "m2"})]


def test_files_from_before_versioning_count_as_version_one(tmp_path):
    (tmp_path / "proposals").mkdir()
    (tmp_path / "proposals" / "shop-1.txt").write_text("old pitch", encoding="utf-8")
    assert [(v.number, v.meta) for v in proposals(tmp_path, "shop-1")] == [(1, {})]
    assert save_proposal(tmp_path, "shop-1", "new pitch", {}).path.name == "shop-1-v2.txt"


def test_gigs_whose_ids_share_a_prefix_stay_apart(tmp_path):
    save_screenshot(tmp_path, "shop-1", b"a", {})
    save_screenshot(tmp_path, "shop-12", b"b", {})
    save_screenshot(tmp_path, "shop-1-extra", b"c", {})
    assert [v.path.name for v in screenshots(tmp_path, "shop-1")] == ["shop-1.png"]


def test_described_screenshot(tmp_path):
    new = save_proposal(tmp_path, "shop-1", "pitch", {"screenshot_version": 3})
    assert described_screenshot(new, []) == 3
    (tmp_path / "proposals" / "old-2.txt").write_text("pitch", encoding="utf-8")
    old = proposals(tmp_path, "old-2")[0]
    # A proposal from before versioning was written with the screenshot saved alongside it, also unversioned.
    (tmp_path / "screenshots").mkdir()
    (tmp_path / "screenshots" / "old-2.png").write_bytes(b"png")
    assert described_screenshot(old, screenshots(tmp_path, "old-2")) == 1
    assert described_screenshot(old, [save_screenshot(tmp_path, "old-3", b"png", {"model": "m"})]) is None
