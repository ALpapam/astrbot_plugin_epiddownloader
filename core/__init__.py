"""P/E 站资源下载插件 - Core Module"""

# 消息匹配正则：.pid 123456 / .eid 123456 / .eid https://e-hentai.org/g/123456/token/
EPID_MESSAGE_PATTERN = r"(?s).*(?:/(?:[Pp][Ii][Dd]|[Ee][Ii][Dd])\s+(\d+))"
