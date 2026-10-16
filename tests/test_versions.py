from dataclasses import asdict, replace
from datetime import datetime, timezone

from fdm.models import Gig
from fdm.store import make_demo_id
from fdm.versions import mentions_screenshot, proposals, save_proposal, saved_gig


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


def test_saved_gig_comes_from_the_newest_snapshot(tmp_path):
    gig = Gig(platform="Freelancer.com", gig_id="77", title="Shop page", description="One page.",
              url="https://example.com/77", skills=["HTML"], budget_max_usd=90.0, pay_type="fixed",
              posted_at=datetime(2026, 10, 13, 10, tzinfo=timezone.utc))
    demo_id = make_demo_id(gig)
    assert saved_gig(tmp_path, demo_id) is None
    save_proposal(tmp_path, demo_id, "old", {"model": "m1"})  # saved before snapshots
    assert saved_gig(tmp_path, demo_id) is None

    save_proposal(tmp_path, demo_id, "one", {"score": 7.5, "gig": asdict(gig)})
    newer = replace(gig, description="One page, plus a contact form.")
    save_proposal(tmp_path, demo_id, "two", {"score": 8.0, "gig": asdict(newer)})
    snapshot = saved_gig(tmp_path, demo_id)
    assert snapshot.gig == newer and snapshot.score == 8.0  # round-trips through JSON, posted_at included


def test_saved_gig_ignores_a_snapshot_of_another_gig(tmp_path):
    other = Gig(platform="Freelancer.com", gig_id="999", title="Shop page", description="", url="https://x.test/9")
    save_proposal(tmp_path, "shop-page-77", "pitch", {"gig": asdict(other)})
    save_proposal(tmp_path, "shop-page-78", "pitch", {"gig": {"title": "missing fields"}})
    assert saved_gig(tmp_path, "shop-page-77") is None and saved_gig(tmp_path, "shop-page-78") is None
