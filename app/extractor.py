from __future__ import annotations

import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit


ALLOWED_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am"}
MEDIA_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp"
}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
COMMENT_LIMIT = 50
POST_KINDS = {"reel", "reels", "p", "tv"}
SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,20}$")
CDN_HOST_SUFFIXES = (".instagram.com", ".cdninstagram.com", ".fbcdn.net")
EMBED_HOST = "https://www.instagram.com"
EMBED_TIMEOUT = 15
EMBED_MAX_BYTES = 512 * 1024 * 1024
EMBED_HTML_MAX_BYTES = 8 * 1024 * 1024
EMBED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ExtractionError(RuntimeError):
    pass


def canonicalize_instagram_url(raw_url: str) -> str:
    value = raw_url.strip()
    if not value:
        raise ValueError("Bitte füge einen Instagram-Link ein.")
    parts = urlsplit(value)
    hostname = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or hostname not in ALLOWED_HOSTS:
        raise ValueError("Es werden nur öffentliche Instagram-Links unterstützt.")
    if not parts.path or parts.path == "/":
        raise ValueError("Der Link führt nicht zu einem Instagram-Beitrag.")
    return urlunsplit(("https", hostname, parts.path.rstrip("/") + "/", "", ""))


def _caption_text(value: object) -> str:
    """Find a caption in gallery-dl/yt-dlp's common Instagram structures."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "caption", "description", "content"):
            if key in value:
                found = _caption_text(value[key])
                if found:
                    return found
        for key in ("edges", "node"):
            if key in value:
                found = _caption_text(value[key])
                if found:
                    return found
    if isinstance(value, list):
        for entry in value:
            found = _caption_text(entry)
            if found:
                return found
    return ""


def _description(data: dict) -> str:
    for key in ("description", "caption", "edge_media_to_caption", "content"):
        if key in data:
            found = _caption_text(data[key])
            if found:
                return found
    for key in ("shortcode_media", "graphql", "node", "post", "items", "entries"):
        nested = data.get(key)
        if isinstance(nested, dict):
            found = _description(nested)
            if found:
                return found
        elif isinstance(nested, list):
            for element in nested:
                if isinstance(element, dict):
                    found = _description(element)
                    if found:
                        return found
    return ""



def _comment_text(entry: dict) -> str:
    for key in ("text", "content", "message"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _comment_author(entry: dict) -> str:
    for key in ("author", "username", "owner", "user"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for sub in ("username", "unique_id", "nickname", "name"):
                if isinstance(value.get(sub), str) and value[sub].strip():
                    return value[sub].strip()
    return ""


def _comment_date(entry: dict) -> str:
    for key in ("created_at", "timestamp", "created_time"):
        value = entry.get(key)
        if isinstance(value, (int, float)) and value > 1_000_000_000:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds")
        if isinstance(value, str) and value.strip():
            return value.strip()[:19]
    return ""


def _collect_comments(value: object, found: list, depth: int) -> None:
    if depth > 8 or len(found) >= 200:
        return
    if isinstance(value, list):
        for entry in value:
            _collect_comments(entry, found, depth + 1)
        return
    if not isinstance(value, dict):
        return
    if _comment_text(value) and _comment_author(value):
        found.append(value)
        return
    for key in ("comments", "preview_comments", "edge_media_to_comment",
                "edge_media_to_parent_comment", "edge_media_preview_comment"):
        if key in value:
            nested = value[key]
            if isinstance(nested, dict):
                nested = nested.get("edges") or nested.get("nodes") or []
            _collect_comments(nested, found, depth + 1)


def _comments(data: dict) -> list[dict]:
    """Best-effort comment extraction from downloader metadata.

    yt-dlp is run with --write-comments, so its info JSON carries the
    public comments Instagram exposes to anonymous requests; the embed
    fallback contributes preview comments found in the embed page.
    """
    found: list = []
    _collect_comments(data, found, depth=0)
    comments: list[dict] = []
    seen: set[str] = set()
    for raw in found:
        text = _comment_text(raw)
        if not text or text in seen:
            continue
        seen.add(text)
        comments.append({
            "author": _comment_author(raw)[:100],
            "text": text[:1000],
            "created_at": _comment_date(raw),
        })
        if len(comments) >= COMMENT_LIMIT:
            break
    return comments

def _read_metadata(directory: Path) -> dict:
    """Prefer a metadata JSON with a caption over an unrelated media sidecar."""
    best: dict = {}
    for path in sorted(directory.rglob("*.json")):
        try:
            if path.stat().st_size > 2_000_000:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if isinstance(data, dict):
            if _description(data):
                return data
            if not best:
                best = data
    return best


def _media_files(directory: Path) -> list[Path]:
    return sorted(
        path for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
    )



# ReelPeek bounded downloads v1
def _bounded_download(command: list[str], directory: Path, label: str) -> dict:
    import os
    import signal
    # Isolate child processes so timeouts also terminate downloader subprocesses.
    try:
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise ExtractionError(f"{label}: Abruf nach 30 Sekunden abgebrochen.")
            if process.returncode != 0 or not _media_files(directory):
                lines = (stderr or stdout).strip().splitlines()
                message = lines[-1][-700:] if lines else "Keine Medien erhalten."
                raise ExtractionError(f"{label}: {message}")
    except OSError as error:
        raise ExtractionError(f"{label}: Downloader konnte nicht ausgeführt werden.") from error
    return _read_metadata(directory)


def _fetch_media(url: str, directory: Path) -> tuple[dict, list[str]]:
    import logging
    import time
    logger = logging.getLogger("uvicorn.error")
    is_reel = urlsplit(url).path.startswith(("/reel/", "/reels/", "/tv/"))
    engines: list[tuple[str, Callable[[str, Path], dict]]] = [
        ("yt-dlp", _run_ytdlp), ("gallery-dl", _run_gallery_dl),
    ]
    if not is_reel:
        engines.reverse()
    (primary_label, primary), (secondary_label, secondary) = engines
    attempts: list[tuple[str, str, Callable[[str, Path], dict]]] = [
        (primary_label, url, primary),
        (secondary_label, url, secondary),
    ]
    # The same shortcode is served under /reel/, /reels/, /p/ and /tv/; the
    # downloaders sometimes succeed on a different path variant.
    variant = _variant_url(url)
    if variant:
        attempts.append((f"{primary_label} [{variant}]", variant, primary))
    # Last resort: the server-rendered embed page still exposes the media of
    # many public posts to anonymous requests.
    attempts.append(("embed", url, _run_embed))
    errors = []
    for label, attempt_url, engine in attempts:
        started = time.monotonic()
        # Never publish files left by a failed/partial download attempt.
        with tempfile.TemporaryDirectory(prefix="attempt-", dir=directory) as work:
            attempt = Path(work)
            try:
                metadata = engine(attempt_url, attempt)
                if not _media_files(attempt):
                    raise ExtractionError(f"{label}: Keine Medien erhalten.")
            except ExtractionError as error:
                errors.append(str(error))
                logger.info("ReelPeek %s fehlgeschlagen nach %.1fs", label, time.monotonic() - started)
                continue
            for entry in list(attempt.iterdir()):
                shutil.move(str(entry), directory / entry.name)
            logger.info("ReelPeek %s erfolgreich nach %.1fs", label, time.monotonic() - started)
            return metadata, errors
    return {}, errors


def _run_gallery_dl(url: str, directory: Path) -> dict:
    return _bounded_download([
        "gallery-dl", "--config-ignore", "--no-input", "--no-colors",
        "--no-mtime", "--write-metadata", "--retries", "0",
        "--http-timeout", "10", "--destination", str(directory), "--", url,
    ], directory, "gallery-dl")


def _run_ytdlp(url: str, directory: Path) -> dict:
    # Prefer combined H.264 media; keep other codecs as fallback.
    # The video compatibility fix normalizes any remaining incompatible media.
    return _bounded_download([
        "yt-dlp", "--ignore-config", "--no-cache-dir", "--no-colors",
        "--no-progress", "--socket-timeout", "10", "--retries", "0",
        "--extractor-retries", "0", "--fragment-retries", "0",
        "--abort-on-unavailable-fragments", "--write-info-json",
        "--write-comments",
        "--no-write-playlist-metafiles",
        "--format", "best[vcodec^=avc1]/best[vcodec=h264]/best",
        "--output", str(directory / "%(autonumber)03d_%(id)s.%(ext)s"),
        "--", url,
    ], directory, "yt-dlp")


def _post_parts(url: str) -> tuple[str, str] | None:
    segments = [part for part in urlsplit(url).path.split("/") if part]
    if (len(segments) >= 2 and segments[0] in POST_KINDS
            and SHORTCODE_RE.match(segments[1])):
        return segments[0], segments[1]
    return None


def _variant_url(url: str) -> str | None:
    found = _post_parts(url)
    if not found:
        return None
    kind, shortcode = found
    alternate = "p" if kind != "p" else "reel"
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{alternate}/{shortcode}/", "", ""))


def _is_instagram_cdn(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return False
    return any(host == suffix.lstrip(".") or host.endswith(suffix)
               for suffix in CDN_HOST_SUFFIXES)


def _meta_content(html_text: str, prop: str) -> str:
    patterns = (
        r'<meta[^>]*property=["\']' + re.escape(prop) + r'["\'][^>]*content=["\']([^"\']*)["\']',
        r'<meta[^>]*content=["\']([^"\']*)["\'][^>]*property=["\']' + re.escape(prop) + r'["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
        if match:
            return unescape(match.group(1)).strip()
    return ""


def _json_string(html_text: str, key: str) -> str:
    match = re.search(r'"' + re.escape(key) + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', html_text)
    if not match:
        return ""
    try:
        return json.loads('"' + match.group(1) + '"')
    except json.JSONDecodeError:
        return ""


def _parse_embed(html_text: str, shortcode: str, page_url: str) -> tuple[dict, str, str]:
    """Extract metadata and the direct media URL from an Instagram embed page."""
    metadata = {
        "id": shortcode,
        "webpage_url": page_url,
        "title": _meta_content(html_text, "og:title"),
        "description": _meta_content(html_text, "og:description"),
        "uploader": _json_string(html_text, "username"),
    }
    for key in ("playable_url_quality_hd", "playable_url", "video_url"):
        value = _json_string(html_text, key)
        if value and _is_instagram_cdn(value):
            return metadata, value, "video"
    for prop in ("og:video:secure_url", "og:video:url", "og:video"):
        value = _meta_content(html_text, prop)
        if value and _is_instagram_cdn(value):
            return metadata, value, "video"
    for key in ("display_url", "display_src"):
        value = _json_string(html_text, key)
        if value and _is_instagram_cdn(value):
            return metadata, value, "image"
    value = _meta_content(html_text, "og:image")
    if value and _is_instagram_cdn(value):
        return metadata, value, "image"
    return metadata, "", ""


class _LoginRedirect(Exception):
    pass


class _LoginRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if "accounts/login" in newurl:
            raise _LoginRedirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get(url: str) -> tuple[bytes, str]:
    req = urlrequest.Request(url, headers={
        "User-Agent": EMBED_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    })
    opener = urlrequest.build_opener(_LoginRedirectHandler)
    with opener.open(req, timeout=EMBED_TIMEOUT) as response:
        final_url = response.geturl()
        if "accounts/login" in final_url:
            raise _LoginRedirect(final_url)
        data = response.read(EMBED_HTML_MAX_BYTES + 1)
    if len(data) > EMBED_HTML_MAX_BYTES:
        raise ExtractionError("embed: HTML-Antwort zu gro\u00df.")
    return data, final_url


def _http_download(url: str, target: Path) -> Path:
    req = urlrequest.Request(url, headers={
        "User-Agent": EMBED_USER_AGENT, "Accept": "*/*",
    })
    with urlrequest.build_opener().open(req, timeout=EMBED_TIMEOUT) as response:
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix not in MEDIA_EXTENSIONS:
            guessed = mimetypes.guess_extension(content_type) or ""
            if guessed in MEDIA_EXTENSIONS:
                suffix = guessed
            elif content_type.startswith("video/"):
                suffix = ".mp4"
            elif content_type.startswith("image/"):
                suffix = ".jpg"
            else:
                raise ExtractionError("embed: Unbekannter Medientyp der Datei.")
        target = target.with_suffix(suffix)
        total = 0
        with target.open("wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > EMBED_MAX_BYTES:
                    raise ExtractionError("embed: Mediendatei \u00fcberschreitet das Größenlimit.")
                handle.write(chunk)
    if total == 0:
        target.unlink(missing_ok=True)
        raise ExtractionError("embed: Mediendatei ist leer.")
    return target


def _json_value(html_text: str, key: str) -> object:
    """Parse the JSON object/array stored under "key" in an HTML-embedded blob."""
    match = re.search(r'"' + re.escape(key) + r'"\s*:\s*', html_text)
    if not match:
        return None
    start = match.end()
    if start >= len(html_text) or html_text[start] not in "[{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(html_text)):
        char = html_text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html_text[start:index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _embed_comments(html_text: str) -> list:
    """Collect preview comments embedded in an Instagram embed page."""
    found: list = []
    for key in ("comments", "edge_media_to_parent_comment",
                "edge_media_to_comment", "preview_comments"):
        _collect_comments(_json_value(html_text, key), found, depth=0)
        if found:
            break
    return found


def _run_embed(url: str, directory: Path) -> dict:
    """Fetch the public embed page and download the media URL it exposes."""
    found = _post_parts(url)
    if not found:
        raise ExtractionError("embed: Link enth\u00e4lt keinen Beitrags-Shortcode.")
    kind, shortcode = found
    problems: list[str] = []
    for embed_kind in dict.fromkeys((kind, "p")):
        embed_url = f"{EMBED_HOST}/{embed_kind}/{shortcode}/embed/captioned/"
        try:
            data, _final_url = _http_get(embed_url)
        except _LoginRedirect:
            raise ExtractionError(
                "embed: Instagram leitet zur Anmeldung weiter "
                f"({EMBED_HOST}/accounts/login/)."
            )
        except (HTTPError, URLError, OSError) as error:
            problems.append(f"{embed_kind}: {error}")
            continue
        html_text = data.decode("utf-8", errors="replace")
        metadata, media_url, _media_kind = _parse_embed(html_text, shortcode, url)
        embed_comments = _embed_comments(html_text)
        if embed_comments:
            metadata["comments"] = embed_comments
        if not media_url:
            if "accounts/login" in html_text:
                raise ExtractionError(
                    "embed: Der Beitrag ist ohne Anmeldung nicht abrufbar."
                )
            problems.append(f"{embed_kind}: kein \u00f6ffentliches Medium gefunden")
            continue
        _http_download(media_url, directory / f"001_{shortcode}")
        return metadata
    detail = "; ".join(problems) if problems else "Abruf fehlgeschlagen"
    raise ExtractionError(f"embed: {detail}")


def _metadata_value(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""



# ReelPeek video compatibility fix v1
def _video_probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(result.stdout)


def _prepare_video(path: Path, backup: bool = False) -> Path:
    """Prepare MP4/H.264 with AAC for browser playback; atomically publish.

    Compatible H.264/yuv420p video is copied without reencoding.
    Other video is encoded with two threads. Images are left untouched.
    backup=True retains a unique original copy when repairing a cached MP4.
    """
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return path
    output = path.with_suffix(".mp4")
    if output != path and output.exists():
        raise ExtractionError("MP4-Zieldatei existiert bereits; nichts überschrieben.")
    try:
        info = _video_probe(path)
        streams = info.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if video is None:
            raise ExtractionError("Die Datei enthält keine Videospur.")
        copy_video = (video.get("codec_name") == "h264"
                      and video.get("pix_fmt") == "yuv420p")
        # Keep working files on /data, not the size-limited /tmp mount.
        with tempfile.TemporaryDirectory(prefix=".video-", dir=path.parent) as work:
            temporary = Path(work) / "ready.mp4"
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                       "-n", "-threads", "2", "-i", str(path),
                       "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn"]
            if copy_video:
                command += ["-c:v", "copy"]
            else:
                command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                            "-pix_fmt", "yuv420p", "-threads", "2",
                            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
            if audio and audio.get("codec_name") != "aac":
                command += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
            else:
                command += ["-c:a", "copy"]
            command += ["-movflags", "+faststart", str(temporary)]
            subprocess.run(command, capture_output=True, text=True,
                           timeout=240, check=True)
            verified = _video_probe(temporary)
            if (not any(s.get("codec_name") == "h264"
                        and s.get("codec_type") == "video"
                        for s in verified.get("streams", []))
                    or float(verified.get("format", {}).get("duration", 0)) <= 0):
                raise ExtractionError("Aufbereitetes Video ist nicht gültig.")
            temporary.chmod(path.stat().st_mode & 0o777)
            if backup:
                with tempfile.NamedTemporaryFile(
                    prefix=path.name + ".before-h264-", suffix=".bak",
                    dir=path.parent, delete=False,
                ) as saved:
                    with path.open("rb") as original:
                        shutil.copyfileobj(original, saved)
                    print("Original gesichert:", saved.name)
            temporary.replace(output)
            if output != path:
                path.unlink()
        return output
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ExtractionError(
            "Video-Aufbereitung fehlgeschlagen; Quelldatei wurde nicht ersetzt. "
            "Bitte Speicherplatz und ffmpeg prüfen."
        ) from error


def extract(url: str, item_id: str, data_root: Path) -> dict:
    media_root = data_root / "media" / item_id
    thumbnail_root = data_root / "thumbnails"
    media_root.mkdir(parents=True, exist_ok=True)
    thumbnail_root.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix=".reelpeek-", dir=data_root) as temporary:
        temp_path = Path(temporary)
        metadata, errors = _fetch_media(url, temp_path)

        files = _media_files(temp_path)
        if not files:
            shutil.rmtree(media_root, ignore_errors=True)
            message = " | ".join(errors)
            raise ExtractionError(
                "Der öffentliche Beitrag konnte momentan nicht geladen werden. "
                "Instagram verlangt möglicherweise eine Anmeldung oder hat den Abruf geändert."
                + (f" Technisches Detail: {message}" if message else "")
            )

        media: list[dict] = []
        image_source: Path | None = None
        video_source: Path | None = None
        for index, source in enumerate(files, start=1):
            suffix = source.suffix.lower()
            target = media_root / f"{index:02d}{suffix}"
            shutil.move(str(source), target)
            target = _prepare_video(target)
            kind = "video" if suffix in VIDEO_EXTENSIONS else "image"
            if kind == "video" and video_source is None:
                video_source = target
            if kind == "image" and image_source is None:
                image_source = target
            media.append(
                {
                    "url": f"/media/{item_id}/{target.name}",
                    "kind": kind,
                    "mime": mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                }
            )

        thumbnail_target = thumbnail_root / f"{item_id}.jpg"
        try:
            if image_source:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(image_source), "-vf", "scale=800:-2", str(thumbnail_target)],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
            elif video_source:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-ss", "0.5", "-i", str(video_source),
                        "-frames:v", "1", "-vf", "scale=800:-2", str(thumbnail_target),
                    ],
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired):
            # A missing thumbnail must not make an otherwise valid post unusable.
            pass

    description = _description(metadata)
    title = _metadata_value(metadata, "title")
    if not title:
        title = (description.splitlines()[0][:120] if description else "Instagram-Beitrag")
    uploader = _metadata_value(metadata, "uploader", "username", "owner_username", "author")
    kinds = {entry["kind"] for entry in media}
    media_kind = "carousel" if len(media) > 1 else next(iter(kinds), "video")
    return {
        "title": title,
        "description": description,
        "uploader": uploader,
        "thumbnail_path": f"/thumbnails/{item_id}.jpg" if thumbnail_target.exists() else None,
        "media": media,
        "media_kind": media_kind,
        "comments": _comments(metadata),
    }
