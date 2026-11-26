from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from bs4 import BeautifulSoup
from pypdf import PdfReader
import pypdfium2 as pdfium
import docx
from docx.document import Document as _Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

SUPPORTED_EXTS = {
    ".pdf", ".docx", ".html", ".htm", ".md", ".txt", ".tex",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".cs", ".cpp", ".c",
    ".go", ".php", ".sh", ".sql", ".css", ".yaml", ".yml",
    ".puml", ".zed", ".vdmsl", ".als", ".json", ".xml",
}

IMAGE_EXTS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

IGNORED_DIRS = {
    ".git", ".idea", ".vscode", "__pycache__", "node_modules",
    "venv", ".venv", "bin", "obj", "dist", "build",
}

MAX_SUBMISSION_CHARS = 350_000
MAX_SUBMISSION_IMAGES = 20
MAX_PDF_RENDER_PAGES = 15


@dataclass
class ImageAsset:
    name: str
    mime_type: str
    data: bytes


def _iter_docx_blocks(parent):
    if isinstance(parent, _Document):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    else:
        raise ValueError("Непідтримуваний контейнер DOCX")

    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def extract_text_from_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    for table in soup.find_all("table"):
        rows_text = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if any(cells):
                rows_text.append(" | ".join(cells))
        table.replace_with("\n[Таблиця]\n" + "\n".join(rows_text) + "\n")

    lines = [line.strip() for line in soup.get_text(separator="\n").splitlines() if line.strip()]
    return "\n".join(lines)


def _extract_images_from_pdf(file_path: Path, display_name: str, limit: int) -> list[ImageAsset]:
    assets: list[ImageAsset] = []
    if limit <= 0:
        return assets

    pdf = pdfium.PdfDocument(str(file_path))
    try:
        pages_count = min(len(pdf), limit, MAX_PDF_RENDER_PAGES)
        for i in range(pages_count):
            page = pdf[i]
            bitmap = page.render(scale=140 / 72)
            pil_image = bitmap.to_pil()
            buf = BytesIO()
            pil_image.save(buf, format="PNG")
            assets.append(
                ImageAsset(
                    name=f"{display_name} [стор. {i + 1}]",
                    mime_type="image/png",
                    data=buf.getvalue(),
                )
            )
    finally:
        pdf.close()

    return assets


def _extract_images_from_docx(doc: docx.Document, display_name: str, limit: int) -> list[ImageAsset]:
    assets: list[ImageAsset] = []
    if limit <= 0:
        return assets

    seen_targets: set[str] = set()
    idx = 0
    for rel in doc.part.rels.values():
        if "image" not in rel.reltype:
            continue
        target_ref = str(rel.target_ref)
        if target_ref in seen_targets:
            continue
        seen_targets.add(target_ref)

        content_type = getattr(rel.target_part, "content_type", "")
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            continue

        idx += 1
        assets.append(
            ImageAsset(
                name=f"{display_name} [рис. {idx}]",
                mime_type=content_type,
                data=rel.target_part.blob,
            )
        )
        if len(assets) >= limit:
            break

    return assets


def extract_text_from_file(file_path: Path) -> str:
    if not file_path.exists():
        raise FileNotFoundError(f"Файл не знайдено: {file_path}")

    suffix = file_path.suffix.lower()

    if suffix == ".pdf":
        reader = PdfReader(str(file_path))
        return "\n".join(
            f"--- Сторінка {i + 1} ---\n{page.extract_text() or ''}"
            for i, page in enumerate(reader.pages)
        )

    if suffix == ".docx":
        doc = docx.Document(str(file_path))
        blocks_text: list[str] = []

        for block in _iter_docx_blocks(doc):
            if isinstance(block, Paragraph):
                text = block.text.strip()
                if text:
                    blocks_text.append(text)
            elif isinstance(block, Table):
                rows_text = []
                for row in block.rows:
                    cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                    if any(cells):
                        rows_text.append(" | ".join(cells))
                if rows_text:
                    blocks_text.append("[Таблиця]\n" + "\n".join(rows_text))

        return "\n".join(blocks_text)

    raw_text = file_path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        return extract_text_from_html(raw_text)

    return raw_text


def _extract_file_with_images(
    file_path: Path,
    display_name: str,
    remaining_images: int,
) -> tuple[str, list[ImageAsset]]:
    suffix = file_path.suffix.lower()
    images: list[ImageAsset] = []

    if suffix == ".pdf":
        text = extract_text_from_file(file_path)
        images = _extract_images_from_pdf(file_path, display_name, remaining_images)
        return text, images

    if suffix == ".docx":
        doc = docx.Document(str(file_path))
        blocks_text: list[str] = []
        for block in _iter_docx_blocks(doc):
            if isinstance(block, Paragraph):
                t = block.text.strip()
                if t:
                    blocks_text.append(t)
            elif isinstance(block, Table):
                rows_text = []
                for row in block.rows:
                    cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                    if any(cells):
                        rows_text.append(" | ".join(cells))
                if rows_text:
                    blocks_text.append("[Таблиця]\n" + "\n".join(rows_text))
        images = _extract_images_from_docx(doc, display_name, remaining_images)
        return "\n".join(blocks_text), images

    return extract_text_from_file(file_path), []


def load_submission_target(
    target_path: Path,
    max_chars: int = MAX_SUBMISSION_CHARS,
    max_images: int = MAX_SUBMISSION_IMAGES,
) -> tuple[str, list[str], list[ImageAsset]]:
    if not target_path.exists():
        raise FileNotFoundError(f"Шлях не існує: {target_path}")

    if target_path.is_file():
        suffix = target_path.suffix.lower()
        if suffix in IMAGE_EXTS:
            asset = ImageAsset(
                name=target_path.name,
                mime_type=IMAGE_EXTS[suffix],
                data=target_path.read_bytes(),
            )
            return f"=== ЗОБРАЖЕННЯ РОБОТИ: {target_path.name} ===", [target_path.name], [asset]

        text, images = _extract_file_with_images(target_path, target_path.name, max_images)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[... ТЕКСТ ОБРІЗАНО ЧЕРЕЗ ЛІМІТ РОЗМІРУ КОНТЕКСТУ ...]"
        return f"=== ФАЙЛ РОБОТИ: {target_path.name} ===\n{text}", [target_path.name], images

    if target_path.is_dir():
        parts: list[str] = []
        loaded_files: list[str] = []
        all_images: list[ImageAsset] = []
        current_chars = 0

        for item in sorted(target_path.rglob("*")):
            if not item.is_file():
                continue
            if any(part in IGNORED_DIRS or part.startswith(".") for part in item.relative_to(target_path).parts):
                continue

            suffix = item.suffix.lower()
            rel_name = str(item.relative_to(target_path))

            if suffix in IMAGE_EXTS:
                if len(all_images) < max_images:
                    all_images.append(
                        ImageAsset(
                            name=rel_name,
                            mime_type=IMAGE_EXTS[suffix],
                            data=item.read_bytes(),
                        )
                    )
                    loaded_files.append(rel_name)
                continue

            if suffix not in SUPPORTED_EXTS:
                continue

            remaining_imgs = max(0, max_images - len(all_images))
            content, file_imgs = _extract_file_with_images(item, rel_name, remaining_imgs)
            all_images.extend(file_imgs)

            block = f"=== ФАЙЛ: {rel_name} ===\n{content}"

            if current_chars + len(block) > max_chars:
                remaining = max(0, max_chars - current_chars)
                if remaining > 200:
                    parts.append(block[:remaining] + "\n[... ОБРІЗАНО ЧЕРЕЗ ЛІМІТ КОНТЕКСТУ ...]")
                    loaded_files.append(f"{rel_name} (частково)")
                break

            parts.append(block)
            loaded_files.append(rel_name)
            current_chars += len(block)

        if not loaded_files and not all_images:
            raise ValueError(f"У папці '{target_path}' не знайдено підтримуваних файлів.")

        return "\n\n".join(parts), loaded_files, all_images

    raise ValueError(f"Непідтримуваний тип шляху: {target_path}")