# Sync iPhone Screenshots

Archives iPhone screenshots from Apple Photos to a date-organized local directory, with JSON sidecars for each image. Also pulls in web downloads and content shared via Messages. Runs incrementally: only processes new photos since the last sync, tracked via SQLite. Safe to interrupt and resume.

In the process, it uncooks cropped screenshots. When you crop a screenshot in Apple Photos, it re-encodes the crop as JPEG — even though the original is a lossless PNG. Fortunately, Photos retains both the original and the lossy crop. This tool recovers a lossless cropped version by using OpenCV template matching to locate the JPEG crop region within the original PNG, then extracting that region with Pillow.

The output directory is designed to feed into [twitter-screenshot-search](https://github.com/wjmallard/twitter-screenshot-search) for OCR and full-text search.

*Vibe-coded with Claude Code.*

## Setup

```
uv sync
cp config.yaml.example config.yaml  # edit to taste
```

## Usage

```
uv run scan
```

Processes all new images since the last sync.

## Config

```yaml
output_dir: ~/Desktop/Screenshots   # where images + DB + sidecars go
workers: 4                          # number of export worker threads
db_name: screenshots.db             # SQLite DB (lives in output_dir)
commit_batch_size: 50               # batch size for database commits
```

## Image classification

Every image in your Photos library is classified and either processed or ignored:

| Condition | Action | `image_source` |
|---|---|---|
| Has camera EXIF (or HEIC format) | Ignore | -- |
| My screenshot, edited (cropped) | Uncook: template match + lossless crop | `cropped_screenshot` |
| My screenshot, unedited | Export as-is | `screenshot` |
| Shared with me via Messages, screenshot | Export as-is (never uncook) | `shared_screenshot` |
| Shared with me via Messages, other | Export as-is | `shared` |
| No camera EXIF, not screenshot | Export as-is | `download` |

Key heuristics:
- **HEIC = always a real photo.** No web content is saved as HEIC. Messaging apps strip camera EXIF but preserve the container format, so HEIC files from friends are correctly ignored.
- **`camera_model is None` = download.** Web-saved images have no camera EXIF. Photos from iPhone friends via iMessage retain camera EXIF. Photos from Android friends via MMS lose it — these get classified as downloads, which is an acceptable tradeoff.
- **`photo.syndicated` = Shared with You.** Content surfaced by the Messages "Shared with You" feature. Exported but never uncooked (we don't have the sender's original).

## Output structure

```
{output_dir}/
  {YYYY}/
    {MM}/
      {YYYYMMDD_HHMMSS}--{original_name}--{shortuuid}.png
      {YYYYMMDD_HHMMSS}--{original_name}--{shortuuid}.png.json
  screenshots.db
  failures.log
```

- Timestamps use the photo's local timezone
- shortuuid is deterministic from the Photos UUID
- JSON sidecars are ExifTool-compatible (written by osxphotos)
- `failures.log` tracks export failures (typically cloud-only syndicated photos)

## How uncooking works

1. Export both the original PNG and the edited JPEG to a temp directory
2. Convert both to grayscale, run `cv2.matchTemplate` (TM_CCOEFF_NORMED)
3. If confidence >= 0.8, extract the matched region from the original PNG using Pillow
4. Save as PNG — lossless, no JPEG artifacts
