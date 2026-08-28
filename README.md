# Local-Wikipedia

Local-Wikipedia is an MCP server that lets you bring Wikipedia offline — search and read articles after a quick one-time download.

Here's why that matters:

1. **Full-text search across Wikipedia**
   - Most MCP servers only support exact title matches and basic redirect handling.
   - Because Local-Wikipedia stores the full text locally, you can run true full-text searches.
2. **Keeps working even without the Internet** after a one-time download
   - Since the data is saved locally, you can search and read Wikipedia articles even when you're offline.
3. **Handles high search frequency**
   - No web API rate limits — iterate and refine your searches as much as you like.
   - Great for use cases that flexibly interpret queries and run repeated searches.
4. **Easy to extend because the full text is local**
   - With the entire corpus on hand, features that are hard on other MCP servers are much easier here.

This MCP server is designed to pair nicely with small local LLMs. Even where large LLMs aren't available, you can still search Wikipedia flexibly and retrieve information. We validated it with a compact, mobile-oriented model called Gemma 3n E4B, and it runs quickly even on CPU-only environments.

To make things small-LLM friendly, we made a few thoughtful choices:

1. A single tool with minimal arguments, so LLMs can call it reliably.
2. Heuristic query correction to clean up over-specified or noisy inputs from an LLM.
3. Concise, situation-aware outputs so it runs fast even with short context windows and limited compute.
4. Advanced DB indexing for fast search and low memory use. Initial setup takes a bit longer, but once it's done, it's snappy.

## Features

This MCP server provides a single tool.

### search_local_wikipedia

Searches for and reads an article by title. Multiple search strategies are unified into a single tool to keep tool use simple for LLMs.

Arguments:

- `title` (required): the article title to look up, e.g. `"Wikipedia"`.
- `length` (optional, default `medium`): how much of the article to return.
  - `very-short` — a brief snippet (~50 words)
  - `short` — a summary (~150 words)
  - `medium` — a detailed summary (~1500 words)
  - `full` — the entire article
- `languages` (optional): a language code or list of language codes to search (e.g. `"en"` or `["en", "simple"]`). Defaults to the languages configured in `config.yaml`.

Search methods, tried in order:

1. Exact title match
2. Exact redirect match
3. Partial title match
4. Partial redirect match
5. Full-text search of the article body

For exact title and redirect matches, the article (or its lead section) is returned. For the other methods, up to 20 results are returned by default (configurable via `max_search_results`).

There's also a heuristic query-fix feature in case an LLM accidentally passes extra or irrelevant details to the tool.

## Setup

Everything is set up with Docker Compose. Follow the steps below.

Make sure Docker and Docker Compose are installed.

```bash
git clone https://github.com/vektorprime/local-wikipedia.git
cd local-wikipedia
docker-compose up
```

On first run, the Wikipedia data for the language specified in `config.yaml` will be downloaded and indexed. This can take a while — roughly tens of minutes for a smaller language and several hours for English (the English build indexes on the order of 19 million pages, 6.6 million full-text documents, and 15 million redirects). We recommend using a stable connection for the download.

Once setup completes, the MCP server will start. By default, it listens on port `29423`. The server uses the MCP **streamable-HTTP** transport, so connect to `http://<host>:29423/mcp` (e.g. `http://localhost:29423/mcp`).

Example MCP client configuration (Open WebUI):

```json
{
  "servers": {
    "local-wikipedia": {
      "url": "http://localhost:29423/mcp"
    }
  }
}
```

## Configuration

Settings live in `config.yaml`:

```yaml
source:
  # Languages to download/index. See https://huggingface.co/datasets/HuggingFaceFW/finewiki for the full list.
  language:
    - en        # English
    # - simple

server:
  # Limit the number of search results (default: 20)
  max_search_results: 20
  # Port for the MCP server (default: 29423)
  port: 29423
```

The `data/` directory (bound to the container at `/app/data`) holds the PostgreSQL database and is where all downloaded and indexed data persists across restarts.

## Technical Details

Local-Wikipedia uses the official Wikipedia dump data for pages and redirects, along with the Markdown-formatted full-text dataset published at [HuggingFaceFW/finewiki](https://huggingface.co/datasets/HuggingFaceFW/finewiki#available-subsets).

Search is backed by PostgreSQL with two complementary index strategies:

- **PGroonga** full-text indexes on document `title` and `text_body` power fast, memory-efficient full-text search (method 5 above) in English and other languages.
- **B-tree** composite indexes on `(title, language_code)` for documents and `(page_title, language_code)` for pages make exact-match and redirect lookups near-instant. Without them, an exact title lookup would sequentially scan millions of rows (~1 second per query); with the b-tree index the same lookup runs in well under a millisecond.

## Important Notes

- The current Local-Wikipedia implementation is not designed for public API access. When exposing the API externally, make appropriate code modifications for security purposes.
- Wikipedia content is licensed under both the Creative Commons Attribution-ShareAlike 4.0 International License (CC BY-SA 4.0) and the GNU Free Documentation License (GFDL).
