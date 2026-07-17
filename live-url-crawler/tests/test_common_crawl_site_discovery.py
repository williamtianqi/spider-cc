import gzip
import io

import common_crawl_site_discovery as c


class TestSafeName:
    def test_replaces_unsafe_chars(self):
        assert c.safe_name("https://ex.com/a b") == "https_ex.com_a_b"

    def test_falls_back_to_default_when_empty(self):
        assert c.safe_name("///") == "site"


class TestPathToUrl:
    def test_passes_through_absolute_url(self):
        assert c.path_to_url("https://data.example/x") == "https://data.example/x"

    def test_prefixes_relative_path(self):
        assert c.path_to_url("crawl/wet/f.warc.wet.gz") == c.DATA_BASE_URL + "crawl/wet/f.warc.wet.gz"

    def test_strips_leading_slash(self):
        assert c.path_to_url("/crawl/f.gz") == c.DATA_BASE_URL + "crawl/f.gz"


class TestIsEnglishDomainCandidate:
    def test_accepts_common_tld(self):
        assert c.is_english_domain_candidate("example.com") is True

    def test_accepts_country_tld(self):
        assert c.is_english_domain_candidate("example.co.uk") is True

    def test_rejects_unknown_tld(self):
        assert c.is_english_domain_candidate("example.ru") is False

    def test_rejects_punycode(self):
        assert c.is_english_domain_candidate("xn--fsq.com") is False

    def test_rejects_non_ascii(self):
        assert c.is_english_domain_candidate("münchen.de") is False


class TestSiteFromTargetUrl:
    def test_basic_site(self):
        site = c.site_from_target_url("https://example.com/some/path")
        assert site["host"] == "example.com"
        assert site["seed_url"] == "https://example.com/"
        assert site["scope"] == "example.com"

    def test_rejects_ip_host(self):
        assert c.site_from_target_url("http://192.168.0.1/x") is None

    def test_rejects_spam_terms(self):
        assert c.site_from_target_url("http://freecasino.com/x") is None

    def test_rejects_host_without_dot(self):
        assert c.site_from_target_url("http://localhost/x") is None

    def test_english_only_filter(self):
        assert c.site_from_target_url("https://example.ru/x", english_domain_only=True) is None
        assert c.site_from_target_url("https://example.com/x", english_domain_only=True) is not None

    def test_http_scheme_preserved(self):
        site = c.site_from_target_url("http://example.com/x")
        assert site["seed_url"] == "http://example.com/"


class TestSelectWetPaths:
    def test_returns_all_when_under_limit(self):
        paths = ["a", "b", "c"]
        assert c.select_wet_paths(paths, 5, False) == paths

    def test_returns_all_when_limit_is_zero(self):
        paths = ["a", "b", "c"]
        assert c.select_wet_paths(paths, 0, True) == paths

    def test_takes_prefix_without_spread(self):
        paths = ["a", "b", "c", "d"]
        assert c.select_wet_paths(paths, 2, False) == ["a", "b"]

    def test_spread_samples_across_list(self):
        paths = [str(i) for i in range(10)]
        selected = c.select_wet_paths(paths, 5, True)
        assert len(selected) == 5
        assert selected[0] == "0"
        assert selected == ["0", "2", "4", "6", "8"]


class TestParseWarcHeaders:
    def _build_wet(self, records):
        buf = io.BytesIO()
        for headers, body in records:
            block = "WARC/1.0\r\n"
            for key, value in headers.items():
                block += f"{key}: {value}\r\n"
            block += f"Content-Length: {len(body)}\r\n"
            block += "\r\n"
            buf.write(block.encode("utf-8"))
            buf.write(body.encode("utf-8"))
            buf.write(b"\r\n")
        return io.BytesIO(buf.getvalue())

    def test_parses_multiple_records(self):
        stream = self._build_wet(
            [
                ({"WARC-Type": "conversion", "WARC-Target-URI": "https://a.com/"}, "body-a"),
                ({"WARC-Type": "conversion", "WARC-Target-URI": "https://b.com/"}, "body-b"),
            ]
        )
        records = list(c.parse_warc_headers(stream))
        targets = [r.get("WARC-Target-URI") for r in records]
        assert targets == ["https://a.com/", "https://b.com/"]

    def test_handles_gzip_stream(self):
        raw = self._build_wet(
            [({"WARC-Type": "conversion", "WARC-Target-URI": "https://a.com/"}, "body-a")]
        ).getvalue()
        compressed = io.BytesIO(gzip.compress(raw))
        with gzip.GzipFile(fileobj=compressed) as gz:
            records = list(c.parse_warc_headers(gz))
        assert records[0]["WARC-Target-URI"] == "https://a.com/"
