from copy import deepcopy

import pytest

from app.media.web_discovery import classify_search_result, classify_search_results, is_page_candidate


def test_details_precede_directory_pdf_and_image_without_changing_original_evidence():
    rows = [
        {"url":"https://esahubble.org/videos/archive/category/3d/viewall/page/2/", "title":"Video Archive: 3D Animations"},
        {"url":"https://esahubble.org/static/archives/techdocs/pdf/hst_tech_0005.pdf#5#7#5", "title":"Black holes"},
        {"url":"https://esahubble.org/videos/heic1507a/", "title":"Fade between photoionised galaxies", "rerank_score":0.8},
        {"url":"https://esahubble.org/images/heic1507g/", "title":"Hubble view of green filament in galaxy Mrk 1498"},
        {"url":"https://esahubble.org/videos/hubblecast43a/", "title":"Hubblecast 43: Hubble and Black Holes", "rerank_score":0.7},
    ]
    original = deepcopy(rows)
    pages = classify_search_results({"results": rows})
    assert [page.kind for page in pages] == ["video_detail", "video_detail", "collection", "image", "document"]
    assert [page.original_position for page in pages] == [2, 4, 0, 3, 1]
    assert rows == original and pages[0].raw == rows[2]
    assert pages[0].url == rows[2]["url"] and "待解析" in pages[0].evidence[0]
    pages[0].raw["title"] = "Changed"
    assert rows == original


@pytest.mark.parametrize("url,title,text", [
    ("https://www.vjshi.com/so/454.html", "城市高清视频素材下载", ""),
    ("https://699pic.com/media/video-4090210.html", "竖屏视频视频素材", "分类 视频 77W视频素材海量下载"),
    ("https://guangtao.taobao.com/topic-141308337", "城市快节奏视觉素材包 - 淘宝逛一逛", ""),
    ("https://www.pexels.com/zh-cn/search/videos/城市/", "最精彩的城市视频", ""),
    ("https://www.58pic.com/tupian-shipin/chengshifengjing.html", "城市风景视频模板素材免费下载", ""),
    ("https://shipin520.com/shipin/104.html", "高清版权竖版视频AE模板免费下载", "甩动秀发 - 00:564K 超清\n字幕条 - 00:291080P\n祝福边框 - 00:201080P"),
    ("https://www.magnific.com/videos/coffee", "Coffee Videos - HD and 4K to download", ""),
    ("https://pixabay.com/videos/search/coffee%20vertical/", "1177+ Free Coffee Vertical 4K & HD Stock Videos", ""),
    ("https://www.pexels.com/search/videos/coffee/", "Free Coffee Videos", ""),
    ("https://coverr.co/stock-video-footage/coffee", "Free Coffee Stock Video Footage", ""),
    ("https://www.vecteezy.com/free-videos/vertical-coffee", "Vertical Coffee Stock Video Footage for Free Download", ""),
    ("https://www.aigei.com/video/index/guan_zhong_16/", "关中平原城市群 视频素材 免费下载", ""),
])
def test_observed_kimi_catalogues_are_not_promoted_by_numeric_url_or_video_word(url, title, text):
    page = classify_search_result({"url":url, "title":title, "text":text})
    assert page.kind == "collection"


def test_aigei_specific_metadata_is_distinct_from_video_directory():
    detail = classify_search_result({"url":"https://www.aigei.com/item/cheng_shi_jing_683.html",
        "title":"城市景观 治愈系唯美视频素材 高清风景(竖版)视频素材(259)",
        "content":"资源编号 : 70189584\n格式 : mp4\n时长 : 07秒\n分辨率 : 720×1280"})
    assert detail.kind == "video_detail" and "待源站核验" in detail.evidence[0]
    unverified = classify_search_result({"url":detail.url, "title":detail.title})
    assert unverified.kind == "unknown"


@pytest.mark.parametrize("row,kind", [
    ({"url":"https://example.org/video/4090210", "title":"Coffee"}, "unknown"),
    ({"url":"https://example.org/video/4090210", "title":"安全验证"}, "challenge"),
    ({"url":"https://example.org/details/1", "title":"Coffee", "mime":"image/jpeg"}, "image"),
    ({"url":"https://example.org/document?id=1", "title":"Coffee", "mime":"application/pdf"}, "document"),
    ({"url":"https://example.org/shot.mp4", "title":"Coffee"}, "unknown"),
    ({"url":"https://www.pexels.com/video/making-coffee-41/", "title":"Making coffee"}, "video_detail"),
    ({"url":"https://pixabay.com/videos/coffee-kitchen-123/", "title":"Coffee in a kitchen"}, "video_detail"),
    ({"url":"https://coverr.co/videos/pouring-coffee-123", "title":"Pouring Coffee Into A Mug"}, "video_detail"),
    ({"url":"https://esahubble.org/videos/heic1507a/", "title":"Videos"}, "collection"),
    ({"url":"https://esahubble.org.evil.test/videos/heic1507a/", "title":"Black holes"}, "unknown"),
    ({"url":"http://localhost/x", "title":"Coffee"}, "invalid"),
    ({"url":"https://[::1]/x", "title":"Coffee"}, "invalid"),
    ({"url":"https://user:secret@example.org/x", "title":"Coffee"}, "invalid"),
    ({"url":"https://example.org:abc/x", "title":"Coffee"}, "invalid"),
    ({"url":None, "title":"Coffee"}, "invalid"),
])
def test_classification_records_only_supported_page_evidence(row, kind):
    page = classify_search_result(row)
    assert page.kind == kind and page.raw == row and page.evidence
    assert not hasattr(page, "download_url")


def test_equal_priority_preserves_provider_order_and_empty_results():
    rows = [{"url":f"https://example.org/{i}", "title":str(i)} for i in range(3)]
    assert [page.raw for page in classify_search_results({"search_results":rows})] == rows
    assert classify_search_results({"results":[]}) == []


@pytest.mark.parametrize("document", [{}, {"results":{}}, {"search_results":[None]}])
def test_bad_envelope_is_explicit(document):
    with pytest.raises(ValueError):
        classify_search_results(document)


@pytest.mark.parametrize("url,title,candidate", [
    ("https://example.org/footage/coffee", "Coffee closeup", True),
    ("https://example.org/", "Coffee closeup", False),
    ("https://example.org/search/coffee", "Coffee", False),
    ("https://example.org/footage/coffee.pdf", "Coffee", False),
    ("https://example.org/footage/coffee", "安全验证", False),
    ("http://127.0.0.1/footage/coffee", "Coffee", False),
])
def test_unknown_specific_page_can_be_inspected_without_becoming_video_detail(url,title,candidate):
    page=classify_search_result({"url":url,"title":title})
    assert is_page_candidate(page) is candidate
    assert page.kind != "video_detail"
