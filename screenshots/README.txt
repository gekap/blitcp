Desktop GUI screenshots for the homepage "See it in action" carousel.

Drop the following PNG files into this directory (public/screenshots/).
The carousel (app/components/Screenshots.tsx) references them by these exact
names and renders them at a 16:10 aspect ratio (object-fit: cover, top-aligned).

Required files:
  transfer.png     — Transfer composer: multiple sources, live plan & progress
  connections.png  — Connections: encrypted S3 / Azure / GCS / SSH profiles
  browse.png       — Browse files: across local, SSH and cloud
  history.png      — History: re-run previous transfers from saved logs

Recommended: ~1600x1000 px (16:10), PNG, dark UI to match the site theme.
