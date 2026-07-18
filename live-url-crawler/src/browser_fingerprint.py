#!/usr/bin/env python3
"""
浏览器 TLS/JA3/JA4 指纹伪装 + UA 轮换。

背景: urllib/aiohttp 的默认 TLS 握手指纹 (基于 Python ssl 模块) 与真实浏览器
完全不同, 任何做 JA3/JA4 指纹检测的 WAF (Cloudflare/Akamai 等) 在 TLS 握手阶段
就能识别出"这不是浏览器", 与 User-Agent 请求头内容无关。

curl_cffi (https://github.com/lexiforest/curl_cffi) 通过 curl-impersonate 在
TLS/HTTP2 层重放真实浏览器指纹, 且 impersonate=<profile> 会自动匹配对应的
User-Agent/sec-ch-ua/Accept-Language 等请求头, 比手工维护 UA 字符串列表更一致、
更不容易被"UA 与 TLS 指纹不匹配"这种二次检测识别。

如果 curl_cffi 未安装, 所有调用方回退到 urllib/aiohttp, 并使用本模块提供的
按域名稳定轮换的真实浏览器 UA 字符串(仅治标, 不解决 TLS 层指纹问题)。
"""
import hashlib

try:
    import curl_cffi  # noqa: F401
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# 当前已安装 curl_cffi 版本实际支持的较新 profile (跨 chrome/firefox/safari/edge 分布,
# 避免全部请求都用同一个指纹). 同一个域名在同一次运行中会稳定映射到同一个 profile
# (真实浏览器同一个会话内 TLS 指纹不会变), 不同域名之间指纹分布是多样的。
IMPERSONATE_PROFILES = [
    "chrome136",
    "chrome131",
    "chrome124",
    "firefox135",
    "firefox133",
    "safari184",
    "safari260",
    "edge101",
]

# curl_cffi 不可用时的降级方案: 真实浏览器 UA 字符串轮换池 (仅伪装请求头, 不涉及 TLS 层)。
FALLBACK_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
]


def _stable_bucket(key, n):
    """把 key 稳定 hash 到 [0, n) 区间, 同一个 key 永远得到同一个下标。"""
    digest = hashlib.md5((key or "default").lower().encode("utf-8")).hexdigest()
    return int(digest, 16) % n


def profile_for_host(host):
    """按域名稳定选择一个 curl_cffi impersonate profile。

    同一个域名在一次爬取(以及跨进程重跑)中始终拿到同一个 profile, 避免
    "同一个会话 TLS 指纹却在跳变"这种更容易被识别的模式; 不同域名之间的
    指纹分布是多样的, 不会让所有流量都长一个样子。
    """
    return IMPERSONATE_PROFILES[_stable_bucket(host, len(IMPERSONATE_PROFILES))]


def user_agent_for_host(host):
    """curl_cffi 不可用时的降级 UA 选择, 同样按域名稳定映射。"""
    return FALLBACK_USER_AGENTS[_stable_bucket(host, len(FALLBACK_USER_AGENTS))]
