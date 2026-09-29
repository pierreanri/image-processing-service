# Image Processing Service

A backend for an image processing service in the spirit of Cloudinary. Users sign up, upload
images, apply transformations (resize, crop, rotate, watermark, flip, mirror, compress, change
format, filters) and download images in different formats.

Built with **FastAPI**, **Pillow**, **SQLAlchemy 2** and **PostgreSQL**. Files are stored on the
local disk.

## Features

- Registration and login with JWT bearer tokens (passwords hashed with Argon2)
- Image upload with validation by file *content* (not filename), size and pixel limits, and EXIF
  orientation handling
- Transformations: crop, resize (`fill`/`contain`/`cover`), rotate, flip, mirror, grayscale, sepia,
  blur, sharpen, text watermark, format conversion and quality-based compression
- Transformations are non-destructive: each one creates a new image linked to its source
- Download images as stored, or converted on the fly with `?format=` and `?quality=`
- Long-lived `Cache-Control` headers and `ETag`/`If-None-Match` (304) support
- Paginated image listing; users can only ever see their own images
- Interactive API docs at `/docs` (Swagger UI) and `/redoc`

## Quick start with Docker

```bash
cp .env.example .env          # then set JWT_SECRET to a long random string
docker compose up --build
```

The API is then available at <http://localhost:8000> and the docs at <http://localhost:8000/docs>.
Database migrations run automatically when the API container starts.

## Local development

Requirements: Python 3.11+ and PostgreSQL.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env          # adjust DATABASE_URL and JWT_SECRET
alembic upgrade head          # create the tables
uvicorn app.main:app --reload
```

To run only the database in Docker: `docker compose up db`.

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

### Tests and linting

The test suite needs a PostgreSQL database it can freely wipe. By default it uses
`postgresql+psycopg://imgsvc:imgsvc@localhost:5432/imgsvc_test`; override it with the
`TEST_DATABASE_URL` environment variable.

```bash
createdb imgsvc_test          # or: docker compose up db, then create it with psql
pytest
ruff check . && ruff format --check .
```

## API

All `/images` endpoints require an `Authorization: Bearer <token>` header. Requesting another
user's image returns `404`. Errors are returned as `{"detail": ...}`.

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
| `quality` | 1–100, for JPEG and WebP (defaults: 85 and 80). PNG is always optimized losslessly. |

Unknown keys are rejected with `422`, so a typo never silently does nothing.

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

### Possible next steps

Caching converted variants (e.g. in Redis), rate limiting transformations, S3-compatible storage,
and moving transformations to a background job queue.
