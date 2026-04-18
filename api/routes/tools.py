import logging
from fnmatch import fnmatch
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import re

from config import settings
from deps import get_user_id, get_pool
from scoped_db import ScopedDB, get_scoped_db

logger = logging.getLogger(__name__)

GUIDE_TEXT = """# LLM Wiki — How It Works

You are connected to an **LLM Wiki** — a personal knowledge workspace where you compile and maintain a structured wiki from raw source documents.

## Architecture

1. **Raw Sources** (path: `/`) — uploaded documents (PDFs, notes, images, spreadsheets). Source of truth. Read-only.
2. **Compiled Wiki** (path: `/wiki/`) — markdown pages YOU create and maintain. You own this layer.
3. **Tools** — `search`, `read`, `write`, `delete` — your interface to both layers.

## Wiki Structure

Every wiki follows this structure. These categories are not suggestions — they are the backbone of the wiki.

### Overview (`/wiki/overview.md`) — THE HUB PAGE
Always exists. This is the front page of the wiki. It must contain:
- A summary of what this wiki covers and its scope
- **Source count** and page count (update on every ingest)
- **Key Findings** — the most important insights across all sources
- **Recent Updates** — last 5-10 actions (ingests, new pages, revisions)

Update the Overview after EVERY ingest or major edit. If you only update one page, it should be this one.

### Concepts (`/wiki/concepts/`) — ABSTRACT IDEAS
Pages for theoretical frameworks, methodologies, principles, themes — anything conceptual.
- `/wiki/concepts/scaling-laws.md`
- `/wiki/concepts/attention-mechanisms.md`
- `/wiki/concepts/self-supervised-learning.md`

Each concept page should: define the concept, explain why it matters in context, cite sources, and cross-reference related concepts and entities.

### Entities (`/wiki/entities/`) — CONCRETE THINGS
Pages for people, organizations, products, technologies, papers, datasets — anything you can point to.
- `/wiki/entities/transformer.md`
- `/wiki/entities/openai.md`
- `/wiki/entities/attention-is-all-you-need.md`

Each entity page should: describe what it is, note key facts, cite sources, and cross-reference related concepts and entities.

### Log (`/wiki/log.md`) — CHRONOLOGICAL RECORD
Always exists. Append-only. Records every ingest, major edit, and lint pass. Never delete entries.

Format — each entry starts with a parseable header:
```
## [YYYY-MM-DD] ingest | Source Title
- Created concept page: [Page Title](concepts/page.md)
- Updated entity page: [Page Title](entities/page.md)
- Updated overview with new findings
- Key takeaway: one sentence summary

## [YYYY-MM-DD] query | Question Asked
- Created new page: [Page Title](concepts/page.md)
- Finding: one sentence answer

## [YYYY-MM-DD] lint | Health Check
- Fixed contradiction between X and Y
- Added missing cross-reference in Z
```

### Additional Pages
You can create pages outside of concepts/ and entities/ when needed:
- `/wiki/comparisons/x-vs-y.md` — for deep comparisons
- `/wiki/timeline.md` — for chronological narratives

But concepts/ and entities/ are the primary categories. When in doubt, file there.

## Page Hierarchy

Wiki pages use a parent/child hierarchy via paths:
- `/wiki/concepts.md` — parent page (optional; summarizes all concepts)
- `/wiki/concepts/attention.md` — child page

Parent pages summarize; child pages go deep. The UI renders this as an expandable tree.

## Writing Standards

**Wiki pages must be substantially richer than a chat response.** They are persistent, curated artifacts.

### Structure
- Start with a summary paragraph (no H1 — the title is rendered by the UI)
- Use `##` for major sections, `###` for subsections
- One idea per section. Bullet points for facts, prose for synthesis.

### Visual Elements — MANDATORY

**Every wiki page MUST include at least one visual element.** A page with only prose is incomplete.

**Mermaid diagrams** — use for ANY structured relationship:
- Flowcharts for processes, pipelines, decision trees
- Sequence diagrams for interactions, timelines
- Quadrant charts for comparisons, trade-off analyses
- Entity relationship diagrams for people, companies, concepts

````
```mermaid
graph LR
    A[Input] --> B[Process] --> C[Output]
```
````

**Tables** — use for ANY structured comparison:
- Feature matrices, pros/cons, timelines, metrics
- If you're listing 3+ items with attributes, it should be a table

**SVG assets** — for custom visuals Mermaid can't express:
- Create: `write(command="create", path="/wiki/", title="diagram.svg", content="<svg>...</svg>", tags=["diagram"])`
- Embed in wiki pages: `![Description](diagram.svg)`

### Citations — REQUIRED

Every factual claim MUST cite its source via markdown footnotes:
```
Transformers use self-attention[^1] that scales quadratically[^2].

[^1]: attention-paper.pdf, p.3
[^2]: scaling-laws.pdf, p.12-14
```

Rules:
- Use the FULL source filename — never truncate
- Add page numbers for PDFs: `paper.pdf, p.3`
- One citation per claim — don't batch unrelated claims
- Citations render as hoverable popover badges in the UI

### Cross-References
Link between wiki pages using standard markdown links to other wiki paths.

## Core Workflows

### Ingest a New Source
1. Read it: `read(path="source.pdf", pages="1-10")`
2. Discuss key takeaways with the user
3. Create or update **concept** pages under `/wiki/concepts/`
4. Create or update **entity** pages under `/wiki/entities/`
5. Update `/wiki/overview.md` — source count, key findings, recent updates
6. Append an entry to `/wiki/log.md`
7. A single source typically touches 5-15 wiki pages — that's expected

### Answer a Question
1. `search(mode="search", query="term")` to find relevant content
2. Read relevant wiki pages and sources
3. Synthesize with citations
4. If the answer is valuable, file it as a new wiki page — explorations should compound
5. Append a query entry to `/wiki/log.md`

### Maintain the Wiki (Lint)
Check for: contradictions, orphan pages, missing cross-references, stale claims, concepts mentioned but lacking their own page. Append a lint entry to `/wiki/log.md`.

## Available Knowledge Bases

"""

router = APIRouter(prefix="/tools", tags=["tools"])

MAX_LIST = 50
MAX_SEARCH = 20

def glob_match(filepath: str, pattern: str) -> bool:
    return fnmatch(filepath, pattern)

def resolve_path(path: str) -> tuple[str, str]:
    path_clean = path.lstrip("/")
    if "/" in path_clean:
        dir_path = "/" + path_clean.rsplit("/", 1)[0] + "/"
        filename = path_clean.rsplit("/", 1)[1]
    else:
        dir_path = "/"
        filename = path_clean
    return dir_path, filename

async def resolve_kb(db: ScopedDB, slug: str) -> dict | None:
    return await db.fetchrow(
        "SELECT id, name, slug FROM knowledge_bases WHERE slug = $1",
        slug,
    )

class SearchRequest(BaseModel):
    knowledge_base: str
    mode: str = "list"
    query: str = ""
    path: str = "*"
    tags: list[str] | None = None
    limit: int = 10

class ReadRequest(BaseModel):
    knowledge_base: str
    path: str
    pages: str = ""
    sections: list[str] | None = None
    include_images: bool = False

class WriteRequest(BaseModel):
    knowledge_base: str
    command: str  # "create", "str_replace", "append"
    path: str = "/"
    title: str = ""
    content: str = ""
    tags: list[str] | None = None
    date_str: str = ""
    old_text: str = ""
    new_text: str = ""

class DeleteRequest(BaseModel):
    knowledge_base: str
    path: str

@router.post("/search")
async def search_tool(
    req: SearchRequest,
    db: Annotated[ScopedDB, Depends(get_scoped_db)]
):
    if not req.knowledge_base:
        kbs = await db.fetch("SELECT name, slug, created_at FROM knowledge_bases ORDER BY created_at DESC")
        if not kbs:
            return "No knowledge bases found. Create one first."

        lines = ["**Knowledge Bases:**\n"]
        for kb in kbs:
            doc_count = await db.fetchval(
                "SELECT count(*) as cnt FROM documents WHERE knowledge_base_id = ("
                "SELECT id FROM knowledge_bases WHERE slug = $1) AND NOT archived",
                kb["slug"],
            )
            cnt = doc_count if doc_count else 0
            lines.append(f"  {kb['slug']}/ — {kb['name']} ({cnt} documents)")
        return "\n".join(lines)

    kb = await resolve_kb(db, req.knowledge_base)
    if not kb:
        raise HTTPException(status_code=404, detail=f"Knowledge base '{req.knowledge_base}' not found.")

    if req.mode == "list":
        docs = await db.fetch(
            "SELECT id, filename, title, path, file_type, tags, page_count, updated_at "
            "FROM documents WHERE knowledge_base_id = $1 AND NOT archived "
            "ORDER BY path, filename",
            kb["id"],
        )
        target = req.path
        if target not in ("*", "**", "**/*"):
            glob_pat = "/" + target.lstrip("/") if not target.startswith("/") else target
            docs = [d for d in docs if glob_match(d["path"] + d["filename"], glob_pat)]

        if req.tags:
            tag_set = {t.lower() for t in req.tags}
            docs = [d for d in docs if tag_set.issubset({t.lower() for t in (d["tags"] or [])})]

        if not docs:
            return f"No matches for `{target}` in {kb['slug']}."

        sources = [d for d in docs if not d["path"].startswith("/wiki/")]
        wiki_pages = [d for d in docs if d["path"].startswith("/wiki/")]

        lines = [f"**{kb['name']}** (`{target}`):\n"]

        if sources:
            lines.append(f"**Sources ({len(sources)}):**")
            for doc in sources[:MAX_LIST]:
                tag_str = f" [{', '.join(doc['tags'])}]" if doc["tags"] else ""
                date_part = f", {doc['updated_at'].strftime('%Y-%m-%d')}" if doc['updated_at'] else ""
                pages_part = f", {doc['page_count']}p" if doc["page_count"] else ""
                lines.append(f"  {doc['path']}{doc['filename']} ({doc['file_type']}{pages_part}{date_part}){tag_str}")
            if len(sources) > MAX_LIST:
                lines.append(f"  ... {len(sources) - MAX_LIST} more")

        if wiki_pages:
            if sources:
                lines.append("")
            lines.append(f"**Wiki ({len(wiki_pages)} pages):**")
            for doc in wiki_pages[:MAX_LIST]:
                date_part = f", {doc['updated_at'].strftime('%Y-%m-%d')}" if doc['updated_at'] else ""
                lines.append(f"  {doc['path']}{doc['filename']}{date_part}")

        return "\n".join(lines)

    elif req.mode == "search":
        if not req.query:
            return "search mode requires a query."

        path_filter = ""
        if req.path not in ("*", "**", "**/*"):
            if req.path.startswith("/wiki"):
                path_filter = " AND d.path LIKE '/wiki/%'"
            elif req.path == "/" or req.path == "/*":
                path_filter = " AND d.path NOT LIKE '/wiki/%'"

        limit = min(req.limit, MAX_SEARCH)
        matches = await db.fetch(
            f"SELECT dc.content, dc.page, dc.header_breadcrumb, dc.chunk_index, "
            f"  d.filename, d.title, d.path, d.file_type, d.tags, "
            f"  pgroonga_score(dc.tableoid, dc.ctid) AS score "
            f"FROM document_chunks dc "
            f"JOIN documents d ON dc.document_id = d.id "
            f"WHERE dc.knowledge_base_id = $1 "
            f"  AND dc.content &@~ $2 "
            f"  AND NOT d.archived"
            f"{path_filter} "
            f"ORDER BY score DESC, dc.chunk_index "
            f"LIMIT {limit}",
            kb["id"], req.query,
        )

        if req.tags:
            tag_set = {t.lower() for t in req.tags}
            matches = [m for m in matches if tag_set.issubset({t.lower() for t in (m.get("tags") or [])})]

        if not matches:
            return f"No matches for `{req.query}` in {kb['slug']}."

        lines = [f"**{len(matches)} result(s)** for `{req.query}`:\n"]

        def _extract_snippet(content: str, query: str) -> str:
            if not content:
                return "(empty)"
            idx = content.lower().find(query.lower())
            if idx < 0:
                return content[:120 * 2].strip()
            start = max(0, idx - 120)
            end = min(len(content), idx + len(query) + 120)
            snippet = content[start:end].strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(content):
                snippet = snippet + "..."
            return snippet

        for m in matches:
            filepath = f"{m['path']}{m['filename']}"
            page_str = f" (p.{m['page']})" if m['page'] else ""
            breadcrumb = f"\n  {m['header_breadcrumb']}" if m["header_breadcrumb"] else ""
            snippet = _extract_snippet(m["content"], req.query)
            link = f"#{m['path']}{m['filename']}"
            score = m.get("score", 0)
            score_str = f" [{score:.1f}]" if score else ""
            lines.append(f"**{filepath}**{page_str}{score_str} — [view]({link}){breadcrumb}")
            lines.append(f"```\n{snippet}\n```\n")

        return "\n".join(lines)

    raise HTTPException(status_code=400, detail=f"Unknown mode: {req.mode}")

@router.post("/guide")
async def guide_tool(
    db: Annotated[ScopedDB, Depends(get_scoped_db)]
):
    kbs = await db.fetch(
        "SELECT name, slug, "
        "  (SELECT count(*) FROM documents d WHERE d.knowledge_base_id = kb.id AND d.path NOT LIKE '/wiki/%' AND NOT d.archived) as source_count, "
        "  (SELECT count(*) FROM documents d WHERE d.knowledge_base_id = kb.id AND d.path LIKE '/wiki/%' AND NOT d.archived) as wiki_count "
        "FROM knowledge_bases kb ORDER BY created_at DESC",
    )
    if not kbs:
        return {"result": GUIDE_TEXT + "No knowledge bases yet. Create one at " + settings.APP_URL + "/wikis"}

    lines = []
    for kb in kbs:
        lines.append(f"- **{kb['name']}** (`{kb['slug']}`) — {kb['source_count']} sources, {kb['wiki_count']} wiki pages")
    return {"result": GUIDE_TEXT + "\n".join(lines)}

@router.post("/read")
async def read_tool(
    req: ReadRequest,
    db: Annotated[ScopedDB, Depends(get_scoped_db)]
):
    kb = await resolve_kb(db, req.knowledge_base)
    if not kb:
        raise HTTPException(status_code=404, detail=f"Knowledge base '{req.knowledge_base}' not found.")

    dir_path, filename = resolve_path(req.path)

    doc = await db.fetchrow(
        "SELECT id, user_id, filename, title, path, content, tags, version, file_type, "
        "page_count, created_at, updated_at "
        "FROM documents WHERE knowledge_base_id = $1 AND filename = $2 AND path = $3 AND NOT archived",
        kb["id"], filename, dir_path,
    )
    if not doc:
        doc = await db.fetchrow(
            "SELECT id, user_id, filename, title, path, content, tags, version, file_type, "
            "page_count, created_at, updated_at "
            "FROM documents WHERE knowledge_base_id = $1 AND (filename = $2 OR title = $2) AND NOT archived",
            kb["id"], req.path.lstrip("/").split("/")[-1],
        )

    if not doc:
        raise HTTPException(status_code=404, detail=f"Document '{req.path}' not found in {req.knowledge_base}.")

    tags_str = ", ".join(doc["tags"]) if doc["tags"] else "none"
    file_type = doc["file_type"] or ""

    header = (
        f"**{doc['title'] or doc['filename']}**\n"
        f"Type: {file_type} | Tags: {tags_str} | Version: {doc['version']} | "
        f"Updated: {doc['updated_at'].strftime('%Y-%m-%d') if doc['updated_at'] else 'unknown'}"
    )
    if doc["page_count"]:
        header += f" | Pages: {doc['page_count']}"
    header += f"\n\n---\n\n"

    # Simple direct content read for now
    content = doc["content"] or ""
    return header + content


@router.post("/write")
async def write_tool(
    req: WriteRequest,
    db: Annotated[ScopedDB, Depends(get_scoped_db)]
):
    kb = await resolve_kb(db, req.knowledge_base)
    if not kb:
        raise HTTPException(status_code=404, detail=f"Knowledge base '{req.knowledge_base}' not found.")

    if req.command == "create":
        if not req.title:
            return "Error: title is required when creating a note."
        if not req.tags:
            return "Error: at least one tag is required when creating a note."

        dir_path = req.path if req.path.endswith("/") else req.path + "/"
        if not dir_path.startswith("/"):
            dir_path = "/" + dir_path

        _title_lower = req.title.lower()
        asset_ext = None
        _ASSET_EXTENSIONS = {".svg", ".csv", ".json", ".xml", ".html"}
        for ext in _ASSET_EXTENSIONS:
            if _title_lower.endswith(ext):
                asset_ext = ext
                break

        if asset_ext:
            filename = re.sub(r"[^\w\s\-.]", "", _title_lower.replace(" ", "-"))
            file_type = asset_ext.lstrip(".")
        else:
            slug = _title_lower
            slug = re.sub(r"\.(md|txt)$", "", slug)
            filename = re.sub(r"[^\w\s\-.]", "", slug.replace(" ", "-"))
            if not filename.endswith(".md"):
                filename += ".md"
            file_type = "md"

        clean_title = re.sub(r"\.(md|txt|svg|csv|json|xml|html)$", "", req.title)
        if clean_title == clean_title.lower() and "-" in clean_title:
            clean_title = clean_title.replace("-", " ").replace("_", " ").strip().title()
        title = clean_title

        from datetime import date
        note_date = req.date_str or date.today().isoformat()

        # Bypass RLS to insert document for user
        pool = await get_pool()
        doc_id = await pool.fetchrow(
            "INSERT INTO documents (knowledge_base_id, user_id, filename, title, path, "
            "file_type, status, content, tags, version) "
            "VALUES ($1, $2, $3, $4, $5, $6, 'ready', $7, $8, 0) "
            "RETURNING id",
            kb["id"], db.user_id, filename, title, dir_path, file_type, req.content, req.tags,
        )

        link = f"#{dir_path}{filename}"

        is_wiki = dir_path.startswith("/wiki/")
        suffix = ""
        if asset_ext:
            suffix = f"\n\nEmbed in wiki pages with: `![{title}]({filename})`"
        elif is_wiki:
            suffix = "\n\nRemember to cite sources using footnotes: `[^1]: source-file.pdf, p.X`"

        return (
            f"Created **{title}** at `{dir_path}{filename}`\n"
            f"Tags: {', '.join(req.tags)} | Date: {note_date}\n"
            f"[View]({link}){suffix}"
        )

    elif req.command == "str_replace":
        if not req.old_text:
            return "Error: old_text is required for str_replace."

        dir_path, filename = resolve_path(req.path)

        doc = await db.fetchrow(
            "SELECT id, content FROM documents "
            "WHERE knowledge_base_id = $1 AND filename = $2 AND path = $3 AND NOT archived",
            kb["id"], filename, dir_path,
        )
        if not doc:
            return f"Document '{req.path}' not found."

        content = doc["content"] or ""
        count = content.count(req.old_text)
        if count == 0:
            return "Error: no match found for old_text."
        if count > 1:
            return f"Error: found {count} matches for old_text. Provide more context to match exactly once."

        new_content = content.replace(req.old_text, req.new_text, 1)
        pool = await get_pool()
        await pool.execute(
            "UPDATE documents SET content = $1, version = version + 1 "
            "WHERE id = $2 AND user_id = $3",
            new_content, doc["id"], db.user_id,
        )

        link = f"#{dir_path}{filename}"
        return f"Edited `{req.path}`. Replaced 1 occurrence.\n[View]({link})"

    elif req.command == "append":
        dir_path, filename = resolve_path(req.path)

        doc = await db.fetchrow(
            "SELECT id, content FROM documents "
            "WHERE knowledge_base_id = $1 AND filename = $2 AND path = $3 AND NOT archived",
            kb["id"], filename, dir_path,
        )
        if not doc:
            return f"Document '{req.path}' not found."

        new_content = (doc["content"] or "") + "\n\n" + req.content
        pool = await get_pool()
        await pool.execute(
            "UPDATE documents SET content = $1, version = version + 1 "
            "WHERE id = $2 AND user_id = $3",
            new_content, doc["id"], db.user_id,
        )

        link = f"#{dir_path}{filename}"
        return f"Appended to `{req.path}`.\n[View]({link})"

    raise HTTPException(status_code=400, detail=f"Unknown command: {req.command}")


@router.post("/delete")
async def delete_tool(
    req: DeleteRequest,
    db: Annotated[ScopedDB, Depends(get_scoped_db)]
):
    kb = await resolve_kb(db, req.knowledge_base)
    if not kb:
        raise HTTPException(status_code=404, detail=f"Knowledge base '{req.knowledge_base}' not found.")

    if not req.path or req.path in ("*", "**", "**/*"):
        return "Error: refusing to delete everything. Use a more specific path."

    is_glob = "*" in req.path or "?" in req.path

    if is_glob:
        docs = await db.fetch(
            "SELECT id, filename, title, path FROM documents "
            "WHERE knowledge_base_id = $1 AND NOT archived ORDER BY path, filename",
            kb["id"],
        )
        glob_pat = "/" + req.path.lstrip("/") if not req.path.startswith("/") else req.path
        matched = [d for d in docs if glob_match(d["path"] + d["filename"], glob_pat)]
    else:
        dir_path, filename = resolve_path(req.path)

        doc = await db.fetchrow(
            "SELECT id, filename, title, path FROM documents "
            "WHERE knowledge_base_id = $1 AND filename = $2 AND path = $3 AND NOT archived",
            kb["id"], filename, dir_path,
        )
        matched = [doc] if doc else []

    if not matched:
        return f"No documents matching `{req.path}` found in {req.knowledge_base}."

    _PROTECTED_FILES = {("/wiki/", "overview.md"), ("/wiki/", "log.md")}
    def _is_protected(doc: dict) -> bool:
        return (doc["path"], doc["filename"]) in _PROTECTED_FILES

    protected = [d for d in matched if _is_protected(d)]
    deletable = [d for d in matched if not _is_protected(d)]

    if not deletable:
        names = ", ".join(f"`{d['path']}{d['filename']}`" for d in protected)
        return f"Cannot delete {names} — these are structural wiki pages. Use `write` to edit their content instead."

    doc_ids = [str(d["id"]) for d in deletable]

    pool = await get_pool()
    await pool.execute(
        "UPDATE documents SET archived = true, updated_at = now() "
        "WHERE id = ANY($1::uuid[]) AND user_id = $2",
        doc_ids, db.user_id,
    )

    lines = [f"Deleted {len(deletable)} document(s):\n"]
    for d in deletable:
        lines.append(f"  {d['path']}{d['filename']}")

    if protected:
        names = ", ".join(f"`{d['path']}{d['filename']}`" for d in protected)
        lines.append(f"\nSkipped (protected): {names}")

    return "\n".join(lines)
