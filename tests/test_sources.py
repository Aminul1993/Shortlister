from fdm.sources import parse_feed, parse_freelancer_projects

FREELANCER_PAYLOAD = {
    "status": "success",
    "result": {
        "projects": [
            {
                "id": 40767032,
                "title": "Company Information Website Build",
                "seo_url": "web-development/Company-Information-Website-Build-40767032",
                "description": "Need a 5 page website.",
                "currency": {"code": "INR", "exchange_rate": 0.012},
                "budget": {"minimum": 1500.0, "maximum": 12500.0},
                "type": "fixed",
                "time_submitted": 1791957615,
                "jobs": [{"id": 3, "name": "PHP"}, {"id": 17, "name": "Website Design"}],
            },
            {"id": None, "title": "broken"},
        ]
    },
}

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Build a BMI calculator</title>
    <link>https://gigs.example.com/1</link>
    <guid>gig-1</guid>
    <description>&lt;p&gt;Small tool&lt;/p&gt;&lt;ul&gt;&lt;li&gt;one form&lt;/li&gt;&lt;/ul&gt;</description>
    <pubDate>Tue, 13 Oct 2026 10:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Landing page for a dentist</title>
    <link href="https://jobs.example.org/42"/>
    <id>urn:42</id>
    <summary>One page, contact form.</summary>
    <updated>2026-10-12T09:30:00Z</updated>
  </entry>
</feed>"""


def test_parse_freelancer_projects_converts_budget_and_url():
    gigs = parse_freelancer_projects(FREELANCER_PAYLOAD)
    assert len(gigs) == 1
    gig = gigs[0]
    assert gig.gig_id == "40767032"
    assert gig.url == "https://www.freelancer.com/projects/web-development/Company-Information-Website-Build-40767032"
    assert gig.budget_max_usd == 150.0
    assert gig.skills == ["PHP", "Website Design"]
    assert gig.posted_at is not None and gig.posted_at.year == 2026


def test_parse_rss_strips_html_and_keeps_bullets():
    [gig] = parse_feed(RSS, "https://www.gigs.example.com/feed.rss")
    assert gig.platform == "gigs.example.com"
    assert gig.title == "Build a BMI calculator"
    assert gig.description == "Small tool\n• one form"
    assert gig.posted_at is not None


def test_parse_atom():
    [gig] = parse_feed(ATOM, "https://jobs.example.org/atom")
    assert gig.url == "https://jobs.example.org/42"
    assert gig.description == "One page, contact form."
    assert gig.posted_at is not None and gig.posted_at.day == 12
