import pipeline_domain_crawler as p


class TestDecodeHtmlBytes:
    def test_uses_utf8_by_default(self):
        assert p.decode_html_bytes("héllo".encode(), {}) == "héllo"

    def test_respects_content_type_charset(self):
        data = "café".encode("latin-1")
        decoded = p.decode_html_bytes(data, {"Content-Type": "text/html; charset=latin-1"})
        assert decoded == "café"

    def test_respects_meta_charset(self):
        data = '<meta charset="latin-1">café'.encode("latin-1")
        decoded = p.decode_html_bytes(data, {})
        assert "café" in decoded

    def test_never_raises_on_bad_bytes(self):
        assert isinstance(p.decode_html_bytes(b"\xff\xfe\x00bad", {}), str)


class TestRegexExtractContent:
    def test_extracts_title(self):
        title, _, extractor = p.regex_extract_content("<title>Hello &amp; Bye</title><p>x</p>", "https://ex.com/")
        assert title == "Hello & Bye"
        assert extractor == "regex_inline"

    def test_strips_script_and_style(self):
        html = "<script>secret()</script><style>.a{}</style><p>Visible</p>"
        _, text, _ = p.regex_extract_content(html, "https://ex.com/")
        assert "secret" not in text
        assert "Visible" in text

    def test_unescapes_entities_in_body(self):
        _, text, _ = p.regex_extract_content("<p>a &amp; b</p>", "https://ex.com/")
        assert "a & b" in text


class TestRegexExtractPageLinks:
    def test_extracts_links(self):
        html = '<a href="/page1">1</a><a href="https://ex.com/page2">2</a>'
        result = p.regex_extract_page_links(html, "https://ex.com/")
        assert "https://ex.com/page1" in result["links"]
        assert "https://ex.com/page2" in result["links"]

    def test_classifies_feed_links(self):
        html = '<a href="https://ex.com/blog/rss">feed</a>'
        result = p.regex_extract_page_links(html, "https://ex.com/")
        assert "https://ex.com/blog/rss" in result["feeds"]

    def test_classifies_sitemap_links(self):
        html = '<a href="https://ex.com/sitemap-posts">map</a>'
        result = p.regex_extract_page_links(html, "https://ex.com/")
        assert "https://ex.com/sitemap-posts" in result["sitemaps"]

    def test_drops_plain_xml_that_is_not_feed_or_sitemap(self):
        html = '<a href="https://ex.com/data.xml">data</a>'
        result = p.regex_extract_page_links(html, "https://ex.com/")
        assert "https://ex.com/data.xml" not in result["links"]


class TestFastExtractContent:
    def test_returns_title_and_text(self):
        html = "<html><head><title>T</title></head><body><p>Body text</p></body></html>"
        title, text, extractor = p.fast_extract_content(html, "https://ex.com/")
        assert "T" in title
        assert "Body text" in text
        assert extractor == "fast_fallback_inline"
