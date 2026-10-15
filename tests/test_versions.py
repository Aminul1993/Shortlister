from fdm.versions import mentions_screenshot, proposals, save_proposal


def test_versions_are_numbered_and_never_overwritten(tmp_path):
    first = save_proposal(tmp_path, "shop-1", "one", {"model": "m1"})
    second = save_proposal(tmp_path, "shop-1", "two", {"model": "m2"})
    assert (first.number, first.path.name) == (1, "shop-1.txt")
    assert (second.number, second.path.name) == (2, "shop-1-v2.txt")
    saved = proposals(tmp_path, "shop-1")
    assert [(v.number, v.path.read_text(encoding="utf-8"), v.meta) for v in saved] == [(1, "one", {"model": "m1"}),
                                                                                     (2, "two", {"model": "m2"})]


def test_files_from_before_versioning_count_as_version_one(tmp_path):
    (tmp_path / "proposals").mkdir()
    (tmp_path / "proposals" / "shop-1.txt").write_text("old pitch", encoding="utf-8")
    assert [(v.number, v.meta) for v in proposals(tmp_path, "shop-1")] == [(1, {})]
    assert save_proposal(tmp_path, "shop-1", "new pitch", {}).path.name == "shop-1-v2.txt"


def test_gigs_whose_ids_share_a_prefix_stay_apart(tmp_path):
    save_proposal(tmp_path, "shop-1", "a", {})
    save_proposal(tmp_path, "shop-12", "b", {})
    save_proposal(tmp_path, "shop-1-extra", "c", {})
    assert [v.path.name for v in proposals(tmp_path, "shop-1")] == ["shop-1.txt"]


def test_mentions_screenshot(tmp_path):
    assert mentions_screenshot(save_proposal(tmp_path, "a-1", "pitch", {"screenshot_version": 2}))
    assert not mentions_screenshot(save_proposal(tmp_path, "b-1", "pitch", {"screenshot_version": None}))
    assert not mentions_screenshot(save_proposal(tmp_path, "c-1", "pitch", {"model": "m1"}))
    # Proposals saved before versioning were always written to go with a screenshot.
    (tmp_path / "proposals" / "old-2.txt").write_text("pitch", encoding="utf-8")
    assert mentions_screenshot(proposals(tmp_path, "old-2")[0])
