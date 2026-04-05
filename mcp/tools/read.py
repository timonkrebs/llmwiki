import json
import logging

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.utilities.types import Image

from db import scoped_query, scoped_queryrow
from .helpers import (
    get_user_id, resolve_kb, deep_link, resolve_path,
    load_s3_bytes, parse_page_range, glob_match,
)

logger = logging.getLogger(__name__)

MAX_BATCH_CHARS = 30_000


def _extract_sections(content: str, section_names: list[str]) -> str:
    lines = content.split("\n")
    sections = []
    current_section = None
    current_lines = []

    for line in lines:
        if line.startswith("#"):
            if current_section and current_lines:
                sections.append((current_section, "\n".join(current_lines)))
            heading = line.lstrip("#").strip()
            current_section = heading
            current_lines = [line]
        elif current_section:
            current_lines.append(line)

    if current_section and current_lines:
        sections.append((current_section, "\n".join(current_lines)))

    wanted = {s.lower() for s in section_names}
    matched = [text for name, text in sections if name.lower() in wanted]

    if not matched:
        return f"No sections matching {section_names} found."
    return "\n\n".join(matched)


async def _read_pages(doc: dict, kb: dict, header: str, pages_str: str) -> str | list:
    max_page = doc["page_count"] or 1
    page_nums = parse_page_range(pages_str, max_page)
    if not page_nums:
        return header + f"Invalid page range: {pages_str} (document has {max_page} pages)"

    user_id = str(doc["user_id"])
    doc_id = str(doc["id"])

    page_rows = await scoped_query(
        user_id,
        "SELECT page, content, elements FROM document_pages "
        "WHERE document_id = $1 AND page = ANY($2) ORDER BY page",
        doc["id"], page_nums,
    )

    if not page_rows:
        return header + f"No page data found for pages {pages_str}."

    result_parts: list[str | Image] = [header]
    for row in page_rows:
        result_parts.append(f"**— Page {row['page']} —**\n\n{row['content']}")

        elements = row["elements"]
        if not elements:
            continue
        if isinstance(elements, str):
            elements = json.loads(elements)

        images = elements.get("images", [])
        if not images:
            continue

        for img_meta in images:
            img_id = img_meta.get("id")
            if not img_id:
                continue
            s3_key = f"{user_id}/{doc_id}/images/{img_id}"
            img_bytes = await load_s3_bytes(s3_key)
            if img_bytes:
                fmt = "jpeg" if img_id.endswith((".jpg", ".jpeg")) else "png"
                result_parts.append(Image(data=img_bytes, format=fmt))

    if any(isinstance(p, Image) for p in result_parts):
        return result_parts
    return "\n\n".join(p for p in result_parts if isinstance(p, str))


async def _read_spreadsheet_index(doc: dict, header: str) -> str:
    user_id = str(doc["user_id"])
    page_rows = await scoped_query(
        user_id,
        "SELECT page, content, elements FROM document_pages "
        "WHERE document_id = $1 ORDER BY page",
        doc["id"],
    )
    if not page_rows:
        return header + (doc["content"] or "(no data)")

    lines = [header, "**Sheets:**\n"]
    for row in page_rows:
        elements = row["elements"]
        if isinstance(elements, str):
            elements = json.loads(elements)
        sheet_name = (elements or {}).get("sheet_name", f"Sheet {row['page']}")
        row_count = row["content"].count("\n") if row["content"] else 0
        lines.append(f"  Page {row['page']}: **{sheet_name}** (~{row_count} rows)")
    lines.append(f"\nUse `pages=\"1\"` to read a specific sheet.")
    return "\n".join(lines)


async def _read_batch(user_id: str, kb: dict, path: str) -> str:
    docs = await scoped_query(
        user_id,
        "SELECT id, filename, title, path, content, tags, file_type "
        "FROM documents WHERE knowledge_base_id = $1 AND NOT archived "
        "ORDER BY path, filename",
        kb["id"],
    )

    glob_pat = "/" + path.lstrip("/") if not path.startswith("/") else path
    docs = [d for d in docs if glob_match(d["path"] + d["filename"], glob_pat)]

    if not docs:
        return f"No documents matching `{path}` in {kb['slug']}."

    text_types = {"md", "txt", "csv", "html", "svg", "json", "xml"}
    readable = [d for d in docs if (d["file_type"] or "") in text_types and d["content"]]
    skipped = [d for d in docs if d not in readable]

    if not readable:
        lines = [f"**{len(skipped)} binary/empty file(s)** matching `{path}` — read individually with page ranges."]
        for d in skipped:
            lines.append(f"  {d['path']}{d['filename']} ({d['file_type']})")
        return "\n".join(lines)

    budget = MAX_BATCH_CHARS // len(readable)
    any_truncated = False
    parts = []

    for doc in readable:
        content = doc["content"] or ""
        link = deep_link(kb["slug"], doc["path"], doc["filename"])
        if len(content) > budget:
            content = content[:budget] + "\n\n... (truncated)"
            any_truncated = True
        parts.append(f"### [{doc['path']}{doc['filename']}]({link})\n\n{content}")

    header = f"**{len(readable)} document(s)** matching `{path}`"
    if any_truncated:
        header += f" ({budget:,} char budget per doc)"
    if skipped:
        header += f"\n*{len(skipped)} binary/empty file(s) skipped — read individually with page ranges*"
    header += "\n\n---\n\n"

    return header + "\n\n---\n\n".join(parts)


def register(mcp: FastMCP) -> None:

    @mcp.tool(
        name="read",
        description=(
            "Read document content from the knowledge vault.\n\n"
            "Accepts a single file path OR a glob pattern to batch-read multiple files:\n"
            "- `path=\"notes.md\"` — read one file\n"
            "- `path=\"*.md\"` — read all markdown files in root\n"
            "- `path=\"/wiki/**\"` — read all wiki pages\n"
            "- `path=\"**/*.md\"` — read all markdown files everywhere\n\n"
            "Batch reads concatenate text content (md, txt, csv, etc.) with citable links and truncate at 30k characters. "
            "Binary files (PDFs, images) are listed but must be read individually with page ranges.\n\n"
            "For PDFs and office docs, use `pages` to read specific page ranges (e.g. '1-5', '3').\n"
            "For spreadsheets, each sheet is a page (call without pages first to see sheet names).\n"
            "Images on requested pages are automatically included in the response.\n\n"
            "When reading sources to compile wiki pages, note the filename and page ranges for citation."
        ),
    )
    async def read(
        ctx: Context,
        knowledge_base: str,
        path: str,
        pages: str = "",
        sections: list[str] | None = None,
    ) -> str | list:
        user_id = get_user_id(ctx)

        kb = await resolve_kb(user_id, knowledge_base)
        if not kb:
            return f"Knowledge base '{knowledge_base}' not found."

        is_glob = "*" in path or "?" in path
        if is_glob:
            return await _read_batch(user_id, kb, path)

        dir_path, filename = resolve_path(path)

        doc = await scoped_queryrow(
            user_id,
            "SELECT id, user_id, filename, title, path, content, tags, version, file_type, "
            "page_count, created_at, updated_at "
            "FROM documents WHERE knowledge_base_id = $1 AND filename = $2 AND path = $3 AND NOT archived",
            kb["id"], filename, dir_path,
        )
        if not doc:
            doc = await scoped_queryrow(
                user_id,
                "SELECT id, user_id, filename, title, path, content, tags, version, file_type, "
                "page_count, created_at, updated_at "
                "FROM documents WHERE knowledge_base_id = $1 AND (filename = $2 OR title = $2) AND NOT archived",
                kb["id"], path.lstrip("/").split("/")[-1],
            )

        if not doc:
            return f"Document '{path}' not found in {knowledge_base}."

        tags_str = ", ".join(doc["tags"]) if doc["tags"] else "none"
        link = deep_link(kb["slug"], doc["path"], doc["filename"])
        file_type = doc["file_type"] or ""

        header = (
            f"**{doc['title'] or doc['filename']}**\n"
            f"Type: {file_type} | Tags: {tags_str} | Version: {doc['version']} | "
            f"Updated: {doc['updated_at'].strftime('%Y-%m-%d') if doc['updated_at'] else 'unknown'}"
        )
        if doc["page_count"]:
            header += f" | Pages: {doc['page_count']}"
        header += f"\n[View in Supavault]({link})\n\n---\n\n"

        image_types = {"png", "jpg", "jpeg", "webp", "gif"}
        if file_type in image_types:
            s3_key = f"{doc['user_id']}/{doc['id']}/source.{file_type}"
            img_bytes = await load_s3_bytes(s3_key)
            if img_bytes:
                fmt = "jpeg" if file_type in ("jpg", "jpeg") else file_type
                return [header, Image(data=img_bytes, format=fmt)]
            return header + "(Image could not be loaded from storage)"

        has_pages = file_type in ("pdf", "pptx", "ppt", "docx", "doc", "xlsx", "xls", "csv")
        spreadsheet_types = {"xlsx", "xls", "csv"}

        if has_pages and pages:
            return await _read_pages(doc, kb, header, pages)

        if file_type in spreadsheet_types and not pages:
            return await _read_spreadsheet_index(doc, header)

        content = doc["content"] or ""
        if sections:
            content = _extract_sections(content, sections)

        return header + content
