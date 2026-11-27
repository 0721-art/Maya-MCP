"""Compose real files produced by Maya into a composite image."""

from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from .policy import output_path, project_path, publish_file


def compose_sheet(root, rows, path, overwrite=False, *, allowed_roots=(),
                  allow_external=False, allow_outside_root=False):
    if not 1 <= len(rows) <= 8 or not 1 <= len(rows[0]) <= 8 or any(len(r) != len(rows[0]) for r in rows):
        raise ValueError("Use a rectangular grid of up to 8 rows and 8 columns")
    path_opts = dict(allowed_roots=allowed_roots, allow_external=allow_external,
                     allow_outside_root=allow_outside_root)
    sources = [[project_path(root, item, **path_opts) for item in row] for row in rows]
    target_candidate = project_path(root, path, **path_opts)
    if any(target_candidate == source for row in sources for source in row):
        raise ValueError("The sheet output must differ from input paths")
    sizes = []
    for row in sources:
        for source in row:
            if source.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                raise ValueError(f"Expected a PNG or JPEG input: {source}")
            if not source.is_file():
                raise ValueError(f"Input image does not exist: {source}")
            with Image.open(source) as picture:
                if picture.format not in {"PNG", "JPEG"} or max(picture.size) > 4096:
                    raise ValueError(f"Expected a PNG/JPEG no larger than 4096 pixels per side: {source}")
                sizes.append(picture.size)
    target = output_path(root, path, {".png"}, overwrite, **path_opts)
    cell_width = max(w for w, _ in sizes)
    cell_height = max(h for _, h in sizes)
    width, height = cell_width * len(rows[0]), cell_height * len(rows)
    if width * height > 32_000_000:
        raise ValueError("Composite exceeds 32 million pixels")
    sheet = Image.new("RGB", (width, height), (30, 32, 35))
    for y, row in enumerate(sources):
        for x, source in enumerate(row):
            with Image.open(source) as picture:
                image = picture.convert("RGBA")
                offset = (x * cell_width + (cell_width - image.width) // 2,
                          y * cell_height + (cell_height - image.height) // 2)
                sheet.paste(image, offset, image)
    with TemporaryDirectory(dir=target.parent) as folder:
        temporary = Path(folder) / "sheet.png"
        sheet.save(temporary, format="PNG")
        publish_file(temporary, target, overwrite)
    return {"path": str(target), "bytes": target.stat().st_size, "width": width, "height": height,
            "sources": [[str(source) for source in row] for row in sources]}
