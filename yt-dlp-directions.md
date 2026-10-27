From the repo root, with your environment active:

`micromamba activate instagram-recipe-env`

Download a Reel and its metadata:

`yt-dlp --write-info-json --output "data/working/manual/%(id)s.%(ext)s" "https://www.instagram.com/reel/REEL_ID/"`

For caption/metadata only—no video download:

`yt-dlp --skip-download --write-info-json --output "data/working/manual/%(id)s.%(ext)s" "https://www.instagram.com/reel/REEL_ID/"`

Inspect available formats first:

`yt-dlp -F "https://www.instagram.com/reel/REEL_ID/"`

If yt-dlp is not found, run it through the environment’s Python:

`python -m yt_dlp --skip-download --write-info-json "https://www.instagram.com/reel/REEL_ID/"`

Use only public content you are authorized to download