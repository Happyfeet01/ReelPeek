from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


ALLOWED_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am"}
MEDIA_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp"
}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


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
    engines = [("yt-dlp", _run_ytdlp), ("gallery-dl", _run_gallery_dl)]
    if not is_reel:
        engines.reverse()
    errors = []
    for label, engine in engines:
        started = time.monotonic()
        # Never publish files left by a failed/partial download attempt.
        with tempfile.TemporaryDirectory(prefix="attempt-", dir=directory) as work:
            attempt = Path(work)
            try:
                metadata = engine(url, attempt)
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
        "--no-write-playlist-metafiles",
        "--format", "best[vcodec^=avc1]/best[vcodec=h264]/best",
        "--output", str(directory / "%(autonumber)03d_%(id)s.%(ext)s"),
        "--", url,
    ], directory, "yt-dlp")


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
    }
