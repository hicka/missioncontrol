import research


def test_strip_html_removes_chrome():
    html = """<html><head><style>body{}</style><script>x()</script></head>
    <body><nav>menu</nav><h1>Title</h1><p>First &amp; second.</p>
    <div>More text</div><footer>foot</footer></body></html>"""
    text = research.strip_html(html)
    assert "Title" in text and "First & second." in text
    assert "menu" not in text and "x()" not in text and "foot" not in text


def test_parse_ddg_html_unwraps_redirects():
    page = ('<a class="result__a" href="//duckduckgo.com/l/?uddg='
            'https%3A%2F%2Fexample.com%2Fdocs&amp;rut=abc">Example <b>Docs</b></a>'
            '<a class="result__a" href="https://direct.example.org/">Direct</a>')
    got = research.parse_ddg_html(page)
    assert got[0]["url"] == "https://example.com/docs"
    assert got[0]["title"] == "Example Docs"
    assert got[1]["url"] == "https://direct.example.org/"
