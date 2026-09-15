r"""会话解析测试。

输入取自**真实粘贴过的 Windows cmd 风格 curl**（含 `^"` / `^\^"` / `^%^2F` 三种转义）。
cookie 值和 csrf **全部换成了明显的假值**（AAAA1111 / FACEFEED…），
只保留转义结构 —— 这段是从真实 curl 抄来的，别把真值粘回去 —— 这里要测的就是转义还原。
"""

import unittest

from src.session import CbgAuthError, CbgSession

CMD_STYLE = r'''curl --url ^"https://cbg.huawei.com/isrp/soms/sale-order?t=1789437238932^&locale=zh_CN^" ^
  -H ^"accept: application/json, text/plain, */*^" ^
  -H ^"content-type: application/json^" ^
  -b ^"JSESSIONID=AAAA1111; cbg_wp_lang=zh_CN; lang=zh_CN; hwssot3=12345678901234; HWSTORE-SESSION=ZZZZ; WPSESSIONID=WWWW; Hm_lvt_48e5a2ca=1,2; _ga=GA1.1.1; HMACCOUNT=860F; cebs=1^" ^
  -H ^"origin: https://cbg.huawei.com^" ^
  -H ^"referer: https://cbg.huawei.com/^" ^
  -H ^"role-code: Store_Manager^" ^
  -H ^"sec-ch-ua: ^\^"Chromium^\^";v=^\^"152^\^", ^\^"Not?A_Brand^\^";v=^\^"24^\^"^" ^
  -H ^"x-csrf-token: FACEFEEDFACEFEEDFACEFEEDFACEFEEDFACEFEEDFACEFEEDFACEFEEDFACEFEED^" ^
  --data-raw ^"^{^\^"storeCode^\^":^\^"SCN231409^\^",^\^"timezone^\^":^\^"Asia^%^2FShanghai^\^"^}^"'''

BASH_STYLE = """curl 'https://cbg.huawei.com/isrp/x?t=1' \\
  -H 'accept: application/json' \\
  -b 'JSESSIONID=BBBB2222; hwssot3=111; WPSESSIONID=222' \\
  -H 'x-csrf-token: DEADBEEF1234'"""


class TestFromCurl(unittest.TestCase):
    def test_windows_cmd_style(self):
        s = CbgSession.from_curl(CMD_STYLE)
        self.assertIn("JSESSIONID=AAAA1111", s.cookies)
        self.assertIn("HWSTORE-SESSION=ZZZZ", s.cookies)
        self.assertEqual(s.csrf, "FACEFEEDFACEFEEDFACEFEEDFACEFEEDFACEFEEDFACEFEEDFACEFEEDFACEFEED")

    def test_cmd_style_drops_analytics_cookies(self):
        """埋点 cookie 必须丢掉，否则请求头白白撑到几 KB。"""
        s = CbgSession.from_curl(CMD_STYLE)
        for noise in ("_ga=", "Hm_lvt_", "HMACCOUNT", "cebs", "cbg_wp_lang"):
            self.assertNotIn(noise, s.cookies)
        # 登录态相关的必须留下
        for keep in ("JSESSIONID", "hwssot3", "WPSESSIONID", "HWSTORE-SESSION"):
            self.assertIn(keep, s.cookies)

    def test_bash_style(self):
        s = CbgSession.from_curl(BASH_STYLE)
        self.assertIn("JSESSIONID=BBBB2222", s.cookies)
        self.assertEqual(s.csrf, "DEADBEEF1234")

    def test_missing_csrf_raises(self):
        with self.assertRaises(CbgAuthError) as ctx:
            CbgSession.from_curl(BASH_STYLE.replace("x-csrf-token: DEADBEEF1234", "x-whatever: 1"))
        self.assertIn("x-csrf-token", str(ctx.exception))

    def test_missing_login_cookie_raises(self):
        bad = "-b 'foo=1; bar=2' -H 'x-csrf-token: ABC'"
        with self.assertRaises(CbgAuthError) as ctx:
            CbgSession.from_curl(bad)
        self.assertIn("登录凭据", str(ctx.exception))

    def test_headers_shape(self):
        h = CbgSession.from_curl(BASH_STYLE).headers()
        self.assertEqual(h["role-code"], "Store_Manager")
        self.assertIn("cookie", h)
        self.assertEqual(h["content-type"], "application/json")

    def test_describe_masks_secrets(self):
        d = CbgSession.from_curl(BASH_STYLE).describe()
        self.assertNotIn("DEADBEEF1234", d)
        self.assertIn("…", d)


if __name__ == "__main__":
    unittest.main()
