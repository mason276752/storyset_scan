"""Export images.json and viewer HTML for storyset (mirrors irasutoya exporter).
"""
from pathlib import Path
import json
from .db import Database


def write_images_json(db: Database, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    data = db.export_all()
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_viewer_html(out: Path, embed: bool = False, data: list[dict] | None = None) -> None:
    template = Path(__file__).parent / "viewer.html"
    html = template.read_text(encoding="utf-8")
    if embed and data is not None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        html = html.replace("/*__EMBEDDED_DATA__*/", f"window.__IMAGES__ = {payload};")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


def export(db_path: str, out_json: str | None = None, out_html: str | None = None, embed: bool = False) -> None:
    db = Database(db_path)
    # run migration to remove unused columns (keep data)
    db.migrate_remove_unused_columns()
    data = db.export_all()
    if out_json:
        write_images_json(db, Path(out_json))
    if out_html:
        write_viewer_html(Path(out_html), embed=embed, data=data if embed else None)
    db.close()
