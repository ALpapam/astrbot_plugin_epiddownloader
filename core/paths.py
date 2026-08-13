"""缓存路径管理，对齐 astrbot StarTools.get_data_dir() 模式"""
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_epiddownloader"

# 路径缓存
_data_dir: Path | None = None


def _get_data_dir() -> Path:
    """获取插件数据目录（延迟初始化）"""
    global _data_dir
    if _data_dir is not None:
        return _data_dir

    try:
        from astrbot.api.star import StarTools
        _data_dir = StarTools.get_data_dir(PLUGIN_NAME)
    except Exception:
        _data_dir = Path(__file__).resolve().parents[1] / "data"
        _data_dir.mkdir(parents=True, exist_ok=True)

    return _data_dir


def _ensure_dir(path: Path) -> Path:
    """确保目录存在"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_cache_path() -> Path:
    """获取缓存根目录"""
    return _ensure_dir(_get_data_dir() / "cache")


def get_pixiv_cache_path() -> Path:
    """获取 Pixiv 图片缓存目录"""
    return _ensure_dir(get_cache_path() / "pixiv")


def get_ehentai_cache_path() -> Path:
    """获取 E-Hentai 图片缓存目录"""
    return _ensure_dir(get_cache_path() / "ehentai")


def get_source_cache_path(kind: str) -> Path:
    """根据来源类型返回对应的图片缓存目录

    Args:
        kind: "pixiv" 或 "ehentai"
    """
    if kind == "pixiv":
        return get_pixiv_cache_path()
    return get_ehentai_cache_path()


def get_output_cache_dir() -> Path:
    """获取输出文件缓存目录（ZIP/PDF）"""
    return _ensure_dir(get_cache_path() / "_output")


def _sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符，保留中文/英文/数字/常见符号"""
    import unicodedata
    cleaned = unicodedata.normalize("NFKC", name)
    result = []
    for ch in cleaned:
        if ch.isalnum() or ch in ("_", "-", ".", "(", ")", "[", "]", "（", "）", "【", "】"):
            result.append(ch)
        elif ch.isspace():
            result.append("_")
        else:
            result.append("-")
    sanitized = "".join(result)
    # 合并连续的下划线/横线
    while "__" in sanitized:
        sanitized = sanitized.replace("__", "_")
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    return sanitized[:80].strip("_-")


def get_cached_output_path(kind: str, item_id: str, title: str = "", ext: str = "zip") -> Path:
    """获取缓存的输出文件路径（ZIP/PDF），ext 不含点，如 'zip' 或 'pdf'

    格式: {id}_{sanitized_title}.{ext}
    如: 59580629_初音未来.zip 或 123456_(同人)标题.pdf
    """
    parts = [str(item_id)]
    if title:
        parts.append(_sanitize_filename(title))
    filename = "_".join(parts) + f".{ext}"
    return get_output_cache_dir() / filename
