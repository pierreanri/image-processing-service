# Image Processing Service

A backend for an image processing service in the spirit of Cloudinary. Users sign up, upload
images, apply transformations (resize, crop, rotate, watermark, flip, mirror, compress, change
format, filters) and download images in different formats.

Built with **FastAPI**, **Pillow**, **SQLAlchemy 2** and **PostgreSQL**. Files are stored on the
local disk; **Redis** optionally caches images converted on the fly and enforces per-user limits on
transformations.

## Features

- Registration and login with JWT bearer tokens (passwords hashed with Argon2)
- Image upload with validation by file *content* (not filename), size and pixel limits, and EXIF
  orientation handling
- Transformations: crop, resize (`fill`/`contain`/`cover`), rotate, flip, mirror, grayscale, sepia,
  blur, sharpen, text watermark, format conversion and quality-based compression
- Transformations are non-destructive: each one creates a new image linked to its source
- Download images as stored, or converted on the fly with `?format=` and `?quality=`
- Conversions are cached in Redis when `REDIS_URL` is set; Redis is optional and the service keeps
  working, uncached, without it or while it is down
- Per-user rate limits on transformations (30 per minute and 500 per hour by default), counted in
  Redis; over a limit the API answers `429` with `Retry-After`. Like the cache, the limits need
  Redis and are lifted (not enforced) while it is down
- Long-lived `Cache-Control` headers and `ETag`/`If-None-Match` (304) support
- Paginated image listing; users can only ever see their own images
- Interactive API docs at `/docs` (Swagger UI) and `/redoc`

## Quick start with Docker

```bash
cp .env.example .env          # then set JWT_SECRET to a long random string
docker compose up --build
```

The API is then available at <http://localhost:8000> and the docs at <http://localhost:8000/docs>.
Compose also starts PostgreSQL and Redis (for the conversion cache and the rate-limit counters).
Database migrations run automatically when the API container starts.

## Local development

Requirements: Python 3.11+, PostgreSQL and, optionally, Redis.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # adjust DATABASE_URL and JWT_SECRET
alembic upgrade head          # create the tables
uvicorn app.main:app --reload
```

To run only the backing services in Docker: `docker compose up db redis`. The conversion cache and
the transform rate limits are off until you set `REDIS_URL=redis://localhost:6379/0` in `.env` (the
app logs a warning at startup while limits are configured without it). Without Docker, a suitable
Redis is `redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru --save "" --appendonly no`.

### Configuration

Settings come from environment variables or a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://imgsvc:imgsvc@localhost:5432/imgsvc` | SQLAlchemy database URL |
| `JWT_SECRET` | *(required)* | Secret used to sign tokens, at least 32 characters |
| `JWT_EXPIRE_MINUTES` | `60` | Access token lifetime |
| `STORAGE_DIR` | `./storage` | Directory where image files are stored |
| `MAX_UPLOAD_BYTES` | `10485760` (10 MB) | Largest accepted upload |
| `MAX_DIMENSION` | `10000` | Largest width/height a transformation may produce |
| `MAX_IMAGE_PIXELS` | `50000000` | Largest pixel count accepted on upload |
| `REDIS_URL` | *(empty: both off)* | Redis for the conversion cache and the rate limits: `redis://`, `rediss://` (TLS) or `unix://` |
| `REDIS_TIMEOUT_SECONDS` | `0.25` | Timeout for connecting to Redis and for each read or write |
| `CACHE_TTL_SECONDS` | `86400` (1 day) | How long an image's cached conversions live after the last one was added |
| `CACHE_MAX_ITEM_BYTES` | `5242880` (5 MB) | Conversions larger than this are served but not cached |
| `TRANSFORM_RATE_LIMIT_PER_MINUTE` | `30` | Transformations each user may start per minute; `0` turns this limit off |
| `TRANSFORM_RATE_LIMIT_PER_HOUR` | `500` | Transformations each user may start per hour; `0` turns this limit off |

Options in the query string of `REDIS_URL` override the client settings; don't set
`decode_responses` there (the app refuses to start if you do). An unsupported scheme, a bad port,
or an unknown or invalid option in `REDIS_URL` also stops startup, but an unreachable Redis does
not.

### Tests and linting

The test suite needs a PostgreSQL database it can freely wipe. By default it uses
`postgresql+psycopg://imgsvc:imgsvc@localhost:5432/imgsvc_test`; override it with the
`TEST_DATABASE_URL` environment variable.

It does not need Redis, and it ignores `REDIS_URL` and the rate-limit settings from `.env`. The
tests in `tests/test_cache_redis.py` and `tests/test_ratelimit_redis.py` run against
`TEST_REDIS_URL` (default `redis://localhost:6379/15`) and are skipped when that is unreachable;
they only touch their own random keys and never flush. They are the only tests that run the rate
limiter's Lua script, so run them before changing `app/ratelimit.py`: with
`REQUIRE_REDIS_TESTS=1` they fail instead of being skipped when Redis is missing.

```bash
createdb imgsvc_test          # or: docker compose up db, then create it with psql
pytest -rs                    # -rs shows why any tests were skipped
REQUIRE_REDIS_TESTS=1 pytest  # with Redis running, e.g. docker compose up -d redis
ruff check . && ruff format --check .
```

## API

All `/images` endpoints require an `Authorization: Bearer <token>` header. Requesting another
user's image returns `404`, and too many transformations return `429` (see
[Rate limits](#rate-limits)). Errors are returned as `{"detail": ...}`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/register` | Create an account; returns the user and an access token |
| `POST` | `/login` | Exchange username and password for an access token |
| `POST` | `/images` | Upload an image (multipart field `file`) |
| `GET` | `/images?page=1&limit=10` | List your images, newest first (`limit` ≤ 100) |
| `GET` | `/images/{id}` | Get an image's metadata |
| `GET` | `/images/{id}/content` | Download the image; optional `format` and `quality` query params |
| `POST` | `/images/{id}/transform` | Transform an image into a new image |
| `DELETE` | `/images/{id}` | Delete an image (images derived from it are kept) |
| `GET` | `/health` | Health check |

### Walkthrough

```bash
# Register (or log in with POST /login and the same body)
curl -X POST localhost:8000/register \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice", "password": "correct-horse"}'
# => {"access_token": "eyJ...", "token_type": "bearer", "expires_in": 3600, "user": {...}}

TOKEN=eyJ...

# Upload
curl -X POST localhost:8000/images -H "Authorization: Bearer $TOKEN" -F file=@cat.jpg
```

```json
{
  "id": "bd06a108-6cf7-46fb-aea5-9c4437d80144",
  "parent_id": null,
  "url": "http://localhost:8000/images/bd06a108-6cf7-46fb-aea5-9c4437d80144/content",
  "original_filename": "cat.jpg",
  "format": "jpeg",
  "mime_type": "image/jpeg",
  "width": 1920,
  "height": 1080,
  "size_bytes": 482133,
  "transformations": null,
  "created_at": "2026-09-29T13:58:59.315397Z"
}
```

```bash
# Transform: creates a new image and returns its metadata
curl -X POST localhost:8000/images/$ID/transform \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"transformations": {"resize": {"width": 800}, "filters": {"grayscale": true}, "format": "webp"}}'

# Download as stored, or converted on the fly
curl -H "Authorization: Bearer $TOKEN" localhost:8000/images/$ID/content -o cat.jpg
curl -H "Authorization: Bearer $TOKEN" "localhost:8000/images/$ID/content?format=png" -o cat.png
curl -H "Authorization: Bearer $TOKEN" "localhost:8000/images/$ID/content?quality=40" -o small.jpg

# List
curl -H "Authorization: Bearer $TOKEN" "localhost:8000/images?page=1&limit=10"
# => {"items": [...], "page": 1, "limit": 10, "total": 2, "pages": 1}
```

### Transformations

Send any combination of these (at least one) to `POST /images/{id}/transform`:

```json
{
  "transformations": {
    "crop": {"x": 0, "y": 0, "width": 400, "height": 300},
    "resize": {"width": 800, "height": 600, "fit": "cover"},
    "rotate": 90,
    "flip": true,
    "mirror": true,
    "filters": {"grayscale": true, "sepia": false, "blur": 2.5, "sharpen": true},
    "watermark": {"text": "© Alice", "position": "bottom-right", "opacity": 0.5, "size": 24},
    "format": "webp",
    "quality": 80
  }
}
```

They are applied in a fixed order: **crop → resize → rotate → flip → mirror → filters →
watermark**, then the result is encoded.

| Option | Details |
|---|---|
| `crop` | Pixel rectangle in the source image; `x`/`y` default to 0. Must fit inside the image. |
| `resize` | `width` and/or `height`. With one, the aspect ratio is kept. With both, `fit` decides: `fill` (default) stretches to the exact size, `contain` fits inside the box keeping the aspect ratio, `cover` fills the box and center-crops the overflow. |
| `rotate` | Degrees clockwise, -360 to 360. Right angles are lossless; other angles enlarge the canvas with transparent corners (white for formats without transparency). |
| `flip` / `mirror` | Flip vertically / mirror horizontally. |
| `filters` | `grayscale`, `sepia`, `blur` (Gaussian radius, up to 50) and `sharpen`. |
| `watermark` | Text drawn in white with a dark outline. `position`: `top-left`, `top-right`, `bottom-left`, `bottom-right` or `center`. `size` is the font size in px (scales with the image when unset). |
| `format` | `jpeg`, `png`, `webp`, `gif`, `bmp` or `tiff` (`jpg` and `tif` are accepted too). Defaults to the source format. |
| `quality` | 1–100, for JPEG and WebP (defaults: 85 and 80). Other formats ignore it; PNG is always optimized losslessly. |

Unknown keys are rejected with `422`, so a typo never silently does nothing.

### Rate limits

Only `POST /images/{id}/transform` is limited, per user account: by default 30 transformations per
minute and 500 per hour. A request counts once it passes authentication, validation and the check
that the image is yours, even if the transformation then fails; `401`, `422` and `404` responses
and rejected requests don't count.

Successful transformations carry the current state of each limit (field syntax from the IETF
[RateLimit header fields draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-ratelimit-headers/)):

```
RateLimit-Policy: "minute";q=30;w=60, "hour";q=500;w=3600
RateLimit: "minute";r=18;t=34, "hour";r=460;t=1800
```

`q` is the limit, `w` the window in seconds, `r` the requests left and `t` the seconds until that
window resets. Over a limit the response is `429` with the same fields plus `Retry-After`, the
seconds until every exhausted limit has reset:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 13
RateLimit: "minute";r=0;t=13, "hour";r=469;t=3000

{"detail": "Too many transformations (limit: 30 per minute); retry in 13s"}
```

Clients should tolerate these headers being absent: they are only sent while the limits are
enforced (Redis configured and reachable).

## Design notes

- **Layout**: `app/imaging.py` holds all Pillow logic and has no web or database dependencies;
  `app/storage.py` wraps the filesystem; `app/routers/` holds the HTTP layer; `app/models.py` and
  `alembic/` define the schema.
- **Immutable images**: a transformation never modifies its source; it stores a new image with
  `parent_id` pointing at the source and the applied `transformations` recorded. Because stored
  files never change, downloads can be cached for a long time and ETags are derived from image ids,
  so `304 Not Modified` responses need no file access.
- **Validation**: formats are detected from file contents, uploads are capped by size and pixel
  count (a guard against decompression bombs), and storage paths are generated by the server
  (`{user_id}/{random}.{ext}`), never taken from user input.
- **Animated GIF/WebP**: only the first frame is processed when transforming or converting.
- **Conversion cache** (`app/cache.py`): each image's conversions live in one Redis hash,
  `imgsvc:variants:v1:{image id}`, whose fields are the ETag variants (`webp-qdefault`, `jpeg-q40`,
  …). Lossless formats ignore `quality`, so they get one entry and one ETag. The cache is only read
  after the ownership check, the `304` check and the original-file check, so it can never serve one
  user's image to another. Deleting an image drops its hash; if that fails (Redis down), the
  leftover can't be served and expires with the TTL. Bump `v1` when encoder output changes.
- **Cache memory**: the TTL is refreshed whenever a conversion is added, entries over
  `CACHE_MAX_ITEM_BYTES` are skipped, and the total is bounded by Redis itself. Run Redis with
  `maxmemory` and `allkeys-lru` (or `volatile-lru` on a shared instance, since every key has a
  TTL). With `noeviction`, new conversions stop being cached once it is full.
- **Cache failures fail open**, and a conversion is always served:
  - Redis unreachable or not answering in time: one warning per outage, and Redis is skipped for
    5 seconds (except by deletes, which still try to drop cached data), so an outage doesn't add a
    timeout to every request.
  - Redis answering but refusing writes (full under `noeviction`, or a read-only replica): one
    warning; cached conversions are still served, new ones are not cached.
  - A write that times out (a large conversion on a slow link) is simply not cached.
- **Rate limiting** (`app/ratelimit.py`): each limit is a fixed window that starts with a user's
  first counted transformation and ends when its Redis key
  (`imgsvc:ratelimit:v1:transform:{user id}:minute|hour`) expires, so every API instance agrees.
  One Lua script checks every limit and counts the request against all of them, or against none if
  any is used up. Fixed windows allow a burst of up to twice a limit around a window boundary (e.g.
  59 transformations within a second with the defaults). Limits are per account, don't bound
  concurrency, and `/register` isn't limited. If the connection breaks after Redis ran the script,
  the retry can count that request twice.
- **Rate limits fail open**: while Redis is unreachable, slow or refusing commands, transformations
  are allowed without being counted (one warning per outage, then Redis is skipped for 5 seconds, as
  for the cache). Losing counters (a restart without persistence, eviction, a failover) only resets
  windows early. Keep `allkeys-lru` (or `volatile-lru`); never `volatile-ttl`, which would evict the
  counters before cached conversions. A Redis ACL user needs `+get +incr +expire +pttl +evalsha
  +script|load` on `~imgsvc:*` for the limiter (plus the hash commands for the cache).
- **Redis timeouts**: `REDIS_TIMEOUT_SECONDS` bounds each connection attempt, read and write, for
  both the cache and the limiter. While Redis stops answering, requests already in flight each wait
  up to that long before it is skipped. A hostname with several addresses is tried one address at a
  time, and name resolution isn't covered, so point `REDIS_URL` at an IP or a reliable resolver for a
  remote Redis. The timeout must also cover sending `CACHE_MAX_ITEM_BYTES` to Redis (5 MB in 0.25 s
  needs about 200 Mbit/s); for a remote Redis, raise the timeout or lower the cap.
- **Redis security**: whoever can write to Redis can change the bytes the API serves and lift the
  rate limits. Keep it on a private network, require a password or ACL, and use `rediss://` across
  untrusted networks.

### Possible next steps

S3-compatible storage, and moving transformations to a background job queue.
