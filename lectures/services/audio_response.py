"""Owner views call this helper to support seeking private local audio."""
import mimetypes
import re

from django.http import FileResponse, HttpResponse, StreamingHttpResponse


def audio_response(request, field):
    audio = field.open("rb")
    size = field.size
    content_type = mimetypes.guess_type(field.name)[0] or "application/octet-stream"
    header = request.headers.get("Range") if request.method == "GET" else None
    # Ignore Range when If-Range is present: no validators are exposed here.
    if header and not request.headers.get("If-Range"):
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
        try:
            if not match or not any(match.groups()):
                raise ValueError
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                length = int(last)
                if length <= 0:
                    raise ValueError
                start, end = max(0, size - length), size - 1
            if start > end or start >= size:
                raise ValueError
        except ValueError:
            audio.close()
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
        else:
            def chunks():
                try:
                    audio.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = audio.read(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk
                finally:
                    audio.close()

            response = StreamingHttpResponse(chunks(), status=206, content_type=content_type)
            response["Content-Range"] = f"bytes {start}-{end}/{size}"
            response["Content-Length"] = str(end - start + 1)
            response._resource_closers.append(audio.close)
    else:
        response = FileResponse(audio, content_type=content_type)
    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "private, no-store"
    return response
