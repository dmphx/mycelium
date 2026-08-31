from xml.etree import ElementTree as ET

import prowlarr


def test_usenet_stream_prefers_prowlarr_link_over_direct_enclosure():
    item = ET.fromstring(
        """<item>
        <title>Show.S01E02.Episode.Title.1080p.WEB-DL</title>
        <link>http://prowlarr:9696/7/download?apikey=test</link>
        <guid>https://indexer.invalid/details/123</guid>
        <enclosure url="https://indexer.invalid/getnzb/123" />
        <size>1073741824</size>
        </item>"""
    )

    stream = prowlarr._usenet_stream(item, season=1, indexer_name="Example")

    assert stream is not None
    assert stream.nzb_url == "http://prowlarr:9696/7/download?apikey=test"
