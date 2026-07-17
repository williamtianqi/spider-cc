import domain_link_crawler as d


class TestNormalizeUrl:
    def test_lowercases_host_and_scheme(self):
        assert d.normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"

    def test_strips_fragment(self):
        assert d.normalize_url("https://ex.com/a#section") == "https://ex.com/a"

    def test_drops_default_ports(self):
        assert d.normalize_url("https://ex.com:443/a") == "https://ex.com/a"
        assert d.normalize_url("http://ex.com:80/a") == "http://ex.com/a"

    def test_keeps_non_default_port(self):
        assert d.normalize_url("http://ex.com:8080/a") == "http://ex.com:8080/a"

    def test_collapses_duplicate_slashes(self):
        assert d.normalize_url("https://ex.com/a//b///c") == "https://ex.com/a/b/c"

    def test_sorts_and_filters_query(self):
        result = d.normalize_url("https://ex.com/p?z=1&a=2&utm_source=x&gclid=y")
        assert result == "https://ex.com/p?a=2&z=1"

    def test_resolves_relative_against_base(self):
        assert d.normalize_url("/foo/bar", "https://ex.com/base/") == "https://ex.com/foo/bar"

    def test_rejects_non_http_scheme(self):
        assert d.normalize_url("mailto:a@b.com") is None
        assert d.normalize_url("ftp://ex.com/a") is None

    def test_rejects_static_extensions_by_default(self):
        assert d.normalize_url("https://ex.com/logo.png") is None
        assert d.normalize_url("https://ex.com/app.js") is None

    def test_allows_static_when_requested(self):
        assert d.normalize_url("https://ex.com/sitemap.xml.gz", allow_static=True) is not None

    def test_returns_none_for_empty(self):
        assert d.normalize_url("") is None
        assert d.normalize_url(None) is None


class TestHashes:
    def test_url_hash_is_deterministic(self):
        assert d.url_hash("https://ex.com/a") == d.url_hash("https://ex.com/a")

    def test_url_hash_differs_for_different_urls(self):
        assert d.url_hash("https://ex.com/a") != d.url_hash("https://ex.com/b")

    def test_content_hash_ignores_whitespace_and_case(self):
        assert d.content_hash("Hello   World") == d.content_hash("hello world")

    def test_content_hash_differs_for_different_text(self):
        assert d.content_hash("hello") != d.content_hash("world")


class TestScope:
    def test_host_of(self):
        assert d.host_of("https://Sub.Example.com/x") == "sub.example.com"

    def test_root_url(self):
        assert d.root_url("https://ex.com/a/b?c=1") == "https://ex.com/"

    def test_allowed_host_match(self):
        assert d.in_scope("https://ex.com/x", {"ex.com"}, set(), False) is True

    def test_out_of_scope_host(self):
        assert d.in_scope("https://other.com/x", {"ex.com"}, set(), False) is False

    def test_subdomain_included_when_flag_set(self):
        assert d.in_scope("https://sub.ex.com/x", set(), {"ex.com"}, True) is True

    def test_subdomain_excluded_by_default(self):
        assert d.in_scope("https://sub.ex.com/x", set(), {"ex.com"}, False) is False

    def test_exact_domain_match_without_subdomains(self):
        assert d.in_scope("https://ex.com/x", set(), {"ex.com"}, False) is True


class TestHelpers:
    def test_clean_text_collapses_spaces_and_blank_lines(self):
        assert d.clean_text("a   b\n\n\nc") == "a b\nc"

    def test_header_get_case_insensitive(self):
        headers = {"Content-Type": "text/html"}
        assert d.header_get(headers, "content-type") == "text/html"
        assert d.header_get(headers, "missing", "def") == "def"

    def test_common_sitemap_urls_use_root(self):
        urls = d.common_sitemap_urls("https://ex.com/some/path")
        assert "https://ex.com/sitemap.xml" in urls

    def test_common_feed_urls_use_root(self):
        urls = d.common_feed_urls("https://ex.com/some/path")
        assert "https://ex.com/feed" in urls


class TestLooksLikeXml:
    def test_detects_xml_by_content_type(self):
        assert d.looks_like_xml(b"anything", {"Content-Type": "application/xml"}) is True

    def test_detects_urlset_prefix(self):
        assert d.looks_like_xml(b"  <urlset>...", {}) is True

    def test_rejects_html(self):
        assert d.looks_like_xml(b"<!doctype html><html>", {"Content-Type": "text/html"}) is False


class TestParseSitemapXml:
    def test_parses_urlset(self):
        xml = (
            b'<?xml version="1.0"?>'
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<url><loc>https://ex.com/p1</loc><lastmod>2024-01-01</lastmod></url>"
            b"<url><loc>https://ex.com/p2</loc></url>"
            b"</urlset>"
        )
        sitemaps, pages = d.parse_sitemap_xml(xml)
        assert sitemaps == []
        assert [p["url"] for p in pages] == ["https://ex.com/p1", "https://ex.com/p2"]
        assert pages[0]["lastmod"] == "2024-01-01"

    def test_parses_sitemapindex(self):
        xml = (
            b'<?xml version="1.0"?>'
            b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<sitemap><loc>https://ex.com/sitemap1.xml</loc></sitemap>"
            b"</sitemapindex>"
        )
        sitemaps, pages = d.parse_sitemap_xml(xml)
        assert [s["url"] for s in sitemaps] == ["https://ex.com/sitemap1.xml"]
        assert pages == []

    def test_returns_empty_on_invalid_xml(self):
        assert d.parse_sitemap_xml(b"not xml at all") == ([], [])


class TestParseFeedXml:
    def test_extracts_alternate_links(self):
        xml = (
            b'<?xml version="1.0"?>'
            b'<feed xmlns="http://www.w3.org/2005/Atom">'
            b'<entry><link rel="alternate" href="https://ex.com/post1"/></entry>'
            b"</feed>"
        )
        urls = d.parse_feed_xml(xml, "https://ex.com/")
        assert "https://ex.com/post1" in urls

    def test_returns_empty_on_invalid_xml(self):
        assert d.parse_feed_xml(b"garbage", "https://ex.com/") == []


class TestExtractPageLinks:
    def test_extracts_anchors_and_canonical(self):
        html = (
            '<html><head>'
            '<link rel="canonical" href="https://ex.com/canonical"/>'
            '<link rel="alternate" type="application/rss+xml" href="https://ex.com/feed.xml"/>'
            "</head><body>"
            '<a href="/page1">one</a><a href="https://ex.com/page2">two</a>'
            "</body></html>"
        )
        result = d.extract_page_links(html, "https://ex.com/")
        assert "https://ex.com/page1" in result["links"]
        assert "https://ex.com/page2" in result["links"]
        assert result["canonical"] == "https://ex.com/canonical"
        assert "https://ex.com/feed.xml" in result["feeds"]


class TestExtractContent:
    def test_fallback_extracts_title_and_text(self):
        html = "<html><head><title>My Title</title></head><body><p>Hello world.</p></body></html>"
        title, text, extractor = d.extract_content(html, "https://ex.com/")
        assert "My Title" in title
        assert "Hello world." in text
        assert extractor in {"trafilatura", "fallback"}

    def test_skips_script_and_style(self):
        html = (
            "<html><body>"
            "<script>var x = 'secret';</script>"
            "<style>.a{color:red}</style>"
            "<p>Visible text</p>"
            "</body></html>"
        )
        _, text, _ = d.extract_content(html, "https://ex.com/")
        assert "secret" not in text
        assert "color:red" not in text
        assert "Visible text" in text
