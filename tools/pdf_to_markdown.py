"""Batch document to Markdown converter using pymupdf4llm.

Usage:
    python pdf_to_markdown.py <file_or_folder_or_url> [--output-dir <dir>] [--ocr]

Supported formats: PDF, EPUB, XPS, MOBI, FB2, CBZ, SVG, TXT
Scanned PDFs: use --ocr flag (requires Tesseract OCR installed)
Webpages: pass a URL (requires trafilatura: pip install trafilatura)
Default output: $KNOWLEDGE_DIR/markdown/ (KNOWLEDGE_DIR defaults to ~/knowledge)
"""
import argparse
import os
import sys
from pathlib import Path

DEFAULT_OUTPUT = Path(os.environ.get("KNOWLEDGE_DIR", Path.home() / "knowledge")) / "markdown"
SUPPORTED_EXTS = {".pdf", ".epub", ".xps", ".mobi", ".fb2", ".cbz", ".svg", ".txt"}
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def convert_document(file_path, output_dir, ocr=False):
    import pymupdf4llm
    if ocr:
        os.environ.setdefault("TESSDATA_PREFIX", r"C:\Program Files\Tesseract-OCR\tessdata")
        import pymupdf
        pymupdf.TOOLS.store_shrink(100)
        # Try without OCR first, fall back to OCR if no text extracted
        md_text = pymupdf4llm.to_markdown(str(file_path))
        text_only = md_text.replace("#", "").replace("*", "").replace("-", "").strip()
        if len(text_only) < 100:
            print(f"  {file_path.name}: no text layer detected, running OCR...")
            md_text = pymupdf4llm.to_markdown(str(file_path),
                                              ocr_strategy="ocr_only",
                                              ocr_language="eng",
                                              ocr_path=TESSERACT_PATH)
        else:
            print(f"  {file_path.name}: text layer found, skipping OCR")
    else:
        md_text = pymupdf4llm.to_markdown(str(file_path))
    out_file = output_dir / f"{file_path.stem}.md"
    out_file.write_text(md_text, encoding="utf-8")
    print(f"  {file_path.name} -> {out_file.name} ({len(md_text):,} chars)")
    return out_file


def convert_url(url, output_dir):
    import trafilatura
    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise RuntimeError(f"Failed to download {url}")
    md_text = trafilatura.extract(downloaded, output_format="markdown",
                                  include_tables=True, include_links=True,
                                  include_images=False)
    if not md_text:
        raise RuntimeError(f"No content extracted from {url}")
    # Derive filename from URL
    from urllib.parse import urlparse
    slug = urlparse(url).path.strip("/").replace("/", "_") or "page"
    slug = slug[:80]  # cap length
    out_file = output_dir / f"{slug}.md"
    out_file.write_text(md_text, encoding="utf-8")
    print(f"  {url} -> {out_file.name} ({len(md_text):,} chars)")
    return out_file


def is_url(s):
    return s.startswith("http://") or s.startswith("https://")


def main():
    parser = argparse.ArgumentParser(description="Convert documents/URLs to markdown")
    parser.add_argument("path", help="File, folder, or URL")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT),
                        help="Output directory for markdown files")
    parser.add_argument("--ocr", action="store_true",
                        help="Enable OCR for scanned PDFs (requires Tesseract)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if is_url(args.path):
        try:
            convert_url(args.path, output_dir)
        except Exception as e:
            print(f"  ERROR converting URL: {e}", file=sys.stderr)
            sys.exit(1)
        print("Done.")
        return

    source = Path(args.path)

    if source.is_file() and source.suffix.lower() in SUPPORTED_EXTS:
        files = [source]
    elif source.is_dir():
        files = sorted(f for f in source.iterdir()
                       if f.suffix.lower() in SUPPORTED_EXTS)
    else:
        exts = ", ".join(sorted(SUPPORTED_EXTS))
        print(f"ERROR: {source} is not a supported file ({exts}) or directory",
              file=sys.stderr)
        sys.exit(1)

    if not files:
        print(f"No supported files found. Supported: {', '.join(sorted(SUPPORTED_EXTS))}")
        return

    print(f"Converting {len(files)} file(s) -> {output_dir}")
    for f in files:
        try:
            convert_document(f, output_dir, ocr=args.ocr)
        except Exception as e:
            print(f"  ERROR converting {f.name}: {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
